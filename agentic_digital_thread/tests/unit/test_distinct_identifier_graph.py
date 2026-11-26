# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The measured "different identifiers" fact lives in the knowledge graph (offline).

Onboarding measures same-named identifier columns of one concept that hold
different identifiers (``distinct_identifiers.measure``). The product owner's
rule: a deterministic connection between columns lives in the graph, not in
prompt prose. So:

1. provision writes each measured pair to the graph as one
   ``(:Field)-[:DIFFERENT_IDENTIFIER_FROM]->(:Field)`` edge between the two
   columns' Field nodes, carrying the concept, both counts, both columns'
   shape signatures (the system agent's IDENTIFIER-MISS signature, stored in
   one canonical JSON form whether or not the schema note can print an
   example of it), ``measured_at`` and ``basis = measured``;
2. the schema note is rendered from the record read back from the graph
   (today's text when the record matches; the shape examples are rendered
   from the stored signatures), so a pair the graph does not hold is not
   named;
3. the system agent gets the same records and its identifier-shape guard is
   built from their signatures; the baked note is parsed only for an agent
   configured with no record (one provisioned before the record existed);
4. a re-provision replaces the system's records and removing the system
   removes them.

The graph is ``_field_graph_fake.FieldGraph``, which runs each statement about
the edge as openCypher (only the scoping the statement states narrows it);
the database is a fake ``pg``. The startup restore:
tests/unit/test_distinct_identifier_restore.py. On PostgreSQL:
tests/integration/test_erp_join_keys_pg.py and
tests/integration/test_identifier_shape_guard_pg.py.
"""
from __future__ import annotations

import datetime
import json
import re
import types
from unittest.mock import patch

import pytest

import agents.onboard as onboard
from agents import distinct_identifiers as di
from agents import identifier_shape_guard as g
from agents import metamodel
from agents import schema_format
from agents import system_agent as sa
from agents.models import ProvisionRequest
from tests.unit._field_graph_fake import EDGE, FieldGraph

SID = "sys-a"
SCHEMA = "sys_a"
_DETAILS = {
    "consumption": [("consumption_id", "uuid"), ("lot_id", "uuid"),
                    ("ref_code", "character varying")],
    "lots": [("lot_id", "uuid"), ("lot_code", "character varying")],
    "dispatch_lines": [("line_id", "uuid"), ("ref_code", "character varying"),
                       ("lot_id", "uuid"), ("qty", "integer")],
}
_CM = [
    {"concept": "unit", "field": "consumption.ref_code", "role": "identifier"},
    {"concept": "unit", "field": "dispatch_lines.ref_code", "role": "identifier"},
    {"concept": "material", "field": "consumption.lot_id", "role": "identifier"},
    {"concept": "material", "field": "dispatch_lines.lot_id", "role": "identifier"},
    {"concept": "material", "field": "lots.lot_id", "role": "identifier"},
]
_FKS = [{"from_table": "consumption", "from_field": "lot_id", "to_table": "lots",
         "to_field": "lot_id"},
        {"from_table": "dispatch_lines", "from_field": "lot_id", "to_table": "lots",
         "to_field": "lot_id"}]
_PAIR = {"concept": "unit", "column": "ref_code", "table_a": "consumption",
         "table_b": "dispatch_lines"}
_PAIR_TEXT = ("unit: consumption.ref_code and dispatch_lines.ref_code, "
              "40 and 37 values read, shaped like KX-9999 and QZ-9999")
_FIELD_A = f"{SID}.consumption.ref_code"
_FIELD_B = f"{SID}.dispatch_lines.ref_code"
#: The stored signatures: the IDENTIFIER-MISS signature of KX-0001, QZ-0001
#: and KX 0001, as compact JSON.
_SIG_A = '[["-"],[["prefix","kx"],[4,"digit"]]]'
_SIG_B = '[["-"],[["prefix","qz"],[4,"digit"]]]'
_SIG_SPACED = '[[" "],[["prefix","kx"],[4,"digit"]]]'


def _forms(prefix, n, width=4, start=1):
    return [f"{prefix}-{i:0{width}d}" for i in range(start, start + n)]


def _rows(a=None, b=None):
    a = _forms("KX", 40) if a is None else a
    b = _forms("QZ", 37) if b is None else b
    return [["a", v, False] for v in a] + [["b", v, False] for v in b]


def _fake_pg(rows=None, fail=None):
    rows = _rows() if rows is None else rows

    def pg(schema, sql, params=None, **kw):
        if "information_schema.columns WHERE table_schema = %s AND table_name" in sql:
            return {"rows": [list(c) for c in _DETAILS.get(params[1], [])]}
        if sql.startswith("WITH a AS (SELECT v FROM"):
            if fail is not None:
                return fail
            return {"rows": rows}
        return {"rows": []}
    return pg


def _ctx(**overrides):
    ctx = onboard._ProvisionState(
        body=None, system_id=SID, system_name="System A", plant="", schema=SCHEMA,
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=list(_CM),
        tables_seen=set(_DETAILS))
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _graph(*tables, attached=True):
    graph = FieldGraph()
    for table in tables:
        for column, _type in _DETAILS[table]:
            graph.add_field(SID, table, column, attached=attached)
    return graph


@pytest.fixture(autouse=True)
def _no_samples(monkeypatch):
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    monkeypatch.setattr(onboard, "_measured_foreign_keys", lambda _schema: list(_FKS))


def _desc(monkeypatch, graph, ctx=None, **pg_kw):
    graph.install(monkeypatch)
    monkeypatch.setattr(onboard, "pg", _fake_pg(**pg_kw))
    ctx = ctx or _ctx()
    return ctx, onboard._dp_pg_tables_desc(ctx)


# --------------------------------------------------------------------------- #
# 1. The measured pair is written to the graph
# --------------------------------------------------------------------------- #

def test_each_measured_pair_is_one_edge_between_its_two_field_nodes(monkeypatch):
    graph = _graph(*_DETAILS)
    before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    _desc(monkeypatch, graph)
    (edge,) = graph.edges
    assert (edge["from"], edge["to"]) == (_FIELD_A, _FIELD_B)
    props = dict(edge["props"])
    measured_at = datetime.datetime.fromisoformat(props.pop("measured_at"))
    assert measured_at.utcoffset() == datetime.timedelta(0)
    assert before <= measured_at <= datetime.datetime.now(datetime.timezone.utc)
    assert props == {"concept": "unit", "read_a": 40, "read_b": 37,
                     "signature_a": _SIG_A, "signature_b": _SIG_B, "basis": "measured"}


def test_the_stored_signature_is_the_identifier_miss_signature():
    """The same signature the guard and the IDENTIFIER-MISS sanitizer compute
    (``system_agent._shape_signature``), as JSON."""
    for stored, value in ((_SIG_A, "KX-0042"), (_SIG_B, "QZ-0042"), (_SIG_SPACED, "KX 0042")):
        assert json.loads(stored) == json.loads(json.dumps(sa._shape_signature(value)))


def test_a_shape_the_note_cannot_print_is_still_stored_and_still_guards(monkeypatch):
    # Both sides consistent and different, but side a's shape holds a space, which a
    # baked list entry cannot hold: the note leaves the shapes out, the graph keeps them.
    rows = _rows(a=[f"KX {i:04d}" for i in range(40)])
    graph = _graph(*_DETAILS)
    ctx, desc = _desc(monkeypatch, graph, rows=rows)
    (edge,) = graph.edges
    assert (edge["props"]["signature_a"], edge["props"]["signature_b"]) == (_SIG_SPACED, _SIG_B)
    assert desc.endswith(schema_format.DISTINCT_IDENTIFIERS_PREFIX
                         + "unit: consumption.ref_code and dispatch_lines.ref_code, "
                         "40 and 37 values read.")
    (record,) = ctx.distinct_identifier_records
    assert (record["signature_a"], record["signature_b"]) == (_SIG_SPACED, _SIG_B)
    assert "shape_a" not in record and "shape_b" not in record
    context = g.context_from(desc, records=ctx.distinct_identifier_records)
    wrong = g.check("SELECT c.lot_id FROM consumption c WHERE c.ref_code = 'QZ-0042'", context)
    assert wrong["identifier_check"]["kind"] == "wrong_form"
    assert "(shaped like KX 9999)" in wrong["note"]          # the refusal can print it
    assert _kind("SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX 0042'",
                 context) == "wrong_form"
    assert _kind("SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'QZ-0042'",
                 context) is None


def test_the_write_reaches_each_field_through_the_systems_schema_and_is_parameterized(
        monkeypatch):
    graph = _graph(*_DETAILS)
    _desc(monkeypatch, graph)
    (write,) = [(c, p) for kind, c, p in graph.statements if kind == "write" and "CREATE" in c]
    cypher, params = write
    assert cypher.count("(s:System {id: $sid})-[:HAS_SCHEMA]->") == 1
    assert cypher.count("-[:HAS_FIELD]->") == 2
    assert params["sid"] == SID
    for value in params.values():
        if isinstance(value, str) and value not in ("measured",):
            assert value not in cypher, value


def test_a_raised_driver_statement_timeout_is_a_timeout():
    """What pgdriver raises for SQLSTATE 57014 is a ``timeout``, not ``failed``."""
    from agents import pgdriver

    def _pg(*_a, **_k):
        raise pgdriver.errors.QueryCanceled("canceling statement due to statement timeout",
                                            pgcode="57014")
    pair = {"table_a": "orders", "table_b": "shipments", "column": "batch_id"}
    assert di._read(_pg, "erp_data", pair, {}, 1000) == ("timeout", [])


def test_nothing_measured_makes_no_graph_call(monkeypatch):
    graph = _graph(*_DETAILS)
    for fail in ({"rows": [], "unavailable": True, "error_category": "QueryCanceled"},
                 {"rows": _rows(b=_forms("KX", 37, start=501))}):
        ctx, desc = _desc(monkeypatch, graph, fail=fail)
        assert schema_format.DISTINCT_IDENTIFIERS_PREFIX not in desc
        assert graph.statements == [] and graph.edges == []
        assert ctx.distinct_identifier_records == []


# --------------------------------------------------------------------------- #
# 2. The note is rendered from the graph record
# --------------------------------------------------------------------------- #

def test_the_note_is_rendered_from_the_graph_record_with_todays_text(monkeypatch):
    graph = _graph(*_DETAILS)
    ctx, desc = _desc(monkeypatch, graph)
    assert desc.endswith(schema_format.DISTINCT_IDENTIFIERS_PREFIX + _PAIR_TEXT + ".")
    kinds = [kind for kind, _c, _p in graph.statements]
    assert kinds == ["write", "read"]                       # written, then read back
    (record,) = ctx.distinct_identifier_records
    assert {k: record[k] for k in ("concept", "column", "table_a", "table_b", "read_a",
                                   "read_b", "signature_a", "signature_b", "shape_a",
                                   "shape_b", "basis")} == {
        **_PAIR, "read_a": 40, "read_b": 37, "signature_a": _SIG_A, "signature_b": _SIG_B,
        "shape_a": "KX-9999", "shape_b": "QZ-9999", "basis": "measured"}


def test_the_note_says_what_the_graph_holds(monkeypatch):
    graph = _graph(*_DETAILS)
    held = graph.query
    graph.query = lambda c, p=None: [{**r, "read_a": 41} for r in held(c, p)]
    ctx, desc = _desc(monkeypatch, graph)
    assert "unit: consumption.ref_code and dispatch_lines.ref_code, 41 and 37 values read" \
        in desc
    assert ctx.distinct_identifier_records[0]["read_a"] == 41
    assert ctx.distinct_identifiers[0]["read_a"] == 40      # the measurement, as measured


@pytest.mark.parametrize("graph", [
    _graph("consumption", "lots"),                          # one Field is missing
    _graph(*_DETAILS, attached=False),                      # Fields not under the schema
], ids=["a field is missing", "fields are not under the system's schema"])
def test_a_pair_the_graph_does_not_hold_is_not_named(monkeypatch, graph):
    ctx, desc = _desc(monkeypatch, graph)
    assert graph.edges == []
    assert schema_format.DISTINCT_IDENTIFIERS_PREFIX not in desc
    assert ctx.distinct_identifier_records == []
    markers = onboard._dp_distinct_identifier_markers(ctx)
    assert markers["distinct_identifier_not_stored"] == [_PAIR]
    assert markers["distinct_identifier_columns"] == ctx.distinct_identifiers


def test_no_not_stored_marker_when_the_graph_holds_every_pair(monkeypatch):
    ctx, _desc_ = _desc(monkeypatch, _graph(*_DETAILS))
    assert "distinct_identifier_not_stored" not in onboard._dp_distinct_identifier_markers(ctx)


def test_a_failed_graph_read_fails_the_step_rather_than_bake_an_unbacked_note(monkeypatch):
    from agents.db import GraphQueryError
    graph = _graph(*_DETAILS)

    def unreadable(*_a, **_k):
        raise GraphQueryError("ServiceUnavailable", "down")
    graph.query = unreadable
    with pytest.raises(GraphQueryError):
        _desc(monkeypatch, graph)


def test_rows_of_another_shape_are_not_records():
    good = {"concept": "unit", "column": "ref_code", "column_b": "ref_code",
            "table_a": "consumption", "table_b": "dispatch_lines", "read_a": 40,
            "read_b": 37, "signature_a": _SIG_A, "signature_b": _SIG_B,
            "measured_at": "2026-10-18T00:00:00+00:00", "basis": "measured"}
    assert di.record_from_row(good) == {
        **{k: v for k, v in good.items() if k != "column_b"},
        "shape_a": "KX-9999", "shape_b": "QZ-9999"}           # rendered from the signatures
    for bad in ({**good, "column_b": "other"}, {**good, "read_a": "forty"},
                {**good, "basis": "guess"}, {**good, "table_a": "a b"}, {**good, "concept": ""},
                "not a row", None):
        assert di.record_from_row(bad) is None, bad


@pytest.mark.parametrize("junk", [
    "KX-9999",                                          # an example, not a signature
    "not json", None, 7, "[]",
    '[["-"],[["prefix","kx"]]]',                        # one segment short
    '[["+"],[["prefix","kx"],[4,"digit"]]]',            # not a separator
    '[["-"],[["prefix","ab"],[4,"digit"]]]',            # hex letters are not a prefix
    '[["-"],[["prefix","KX"],[4,"digit"]]]',            # a prefix is compared lowercased
    '[["-"],[["prefix","kx"],[4,"octal"]]]',            # no such class
    '[["-"],[["prefix","kx"],[0,"digit"]]]',            # an empty digit run
    '[["-"],[["prefix","kx"],[true,"digit"]]]',
    '[[" "],[[0,"alnum"],[4,"digit"]]]',                # a leading space is stripped
])
def test_a_signature_the_identifier_miss_signature_cannot_produce_is_not_a_record(junk):
    good = {"concept": "unit", "column": "ref_code", "column_b": "ref_code",
            "table_a": "consumption", "table_b": "dispatch_lines", "read_a": 40,
            "read_b": 37, "signature_a": _SIG_A, "signature_b": junk,
            "measured_at": "2026-10-18T00:00:00+00:00", "basis": "measured"}
    # A record the guard cannot use is no record: it must never stand in for one.
    assert di.record_from_row(good) is None
    assert di.record_from_row({**good, "signature_b": _SIG_B})["signature_b"] == _SIG_B


_SIGNED_EDGE = {"concept": "unit", "read_a": 40, "read_b": 37, "signature_a": _SIG_A,
                "signature_b": _SIG_B, "measured_at": "2026-10-18T00:00:00+00:00",
                "basis": "measured"}
_UNUSABLE_EDGES = {
    "one signature missing": {k: v for k, v in _SIGNED_EDGE.items() if k != "signature_b"},
    "one signature invalid": {**_SIGNED_EDGE, "signature_b": "not a signature"},
    # An earlier build wrote shape examples and no signature.
    "a shape-only edge": {**{k: v for k, v in _SIGNED_EDGE.items()
                          if not k.startswith("signature_")},
                       "shape_a": "KX-9999", "shape_b": "QZ-9999"},
}


@pytest.mark.parametrize("props", list(_UNUSABLE_EDGES.values()), ids=list(_UNUSABLE_EDGES))
def test_a_stored_edge_without_two_valid_signatures_fails_the_record_read(monkeypatch, props):
    """Such an edge is not silently read as a record (it would switch the
    prose fallback off) nor skipped (that reads as "no record"): the read fails."""
    graph = _graph(*_DETAILS).install(monkeypatch)
    graph.add_edge(_FIELD_A, _FIELD_B, **props)
    with pytest.raises(Exception) as raised:
        onboard.read_distinct_identifier_records(SID)
    assert type(raised.value).__name__ == "UnusableRecordError"
    assert issubclass(type(raised.value), ValueError)


def test_a_valid_edge_beside_an_unusable_one_still_fails_the_read(monkeypatch):
    graph = _graph(*_DETAILS).install(monkeypatch)
    graph.add_edge(_FIELD_A, _FIELD_B, **_SIGNED_EDGE)
    graph.add_edge(f"{SID}.consumption.lot_id", f"{SID}.dispatch_lines.lot_id",
                   **_UNUSABLE_EDGES["a shape-only edge"])
    with pytest.raises(Exception) as raised:
        onboard.read_distinct_identifier_records(SID)
    assert type(raised.value).__name__ == "UnusableRecordError"


def test_a_measured_pair_without_signatures_is_never_written(monkeypatch):
    graph = _graph(*_DETAILS).install(monkeypatch)
    ctx = _ctx(distinct_identifiers=[{**_PAIR, "read_a": 40, "read_b": 37,
                                      "shape_a": "KX-9999", "shape_b": "QZ-9999"}])
    records = onboard._dp_store_distinct_identifiers(ctx)
    assert graph.edges == [] and records == []
    assert ctx.distinct_identifiers_not_stored == [_PAIR]


def test_the_stored_signature_is_the_shape_source():
    row = {"concept": "unit", "column": "ref_code", "column_b": "ref_code",
           "table_a": "consumption", "table_b": "dispatch_lines", "read_a": 40,
           "read_b": 37, "signature_a": '[["-"], [["prefix", "kx"], [4, "digit"]]]',
           "signature_b": _SIG_B, "shape_a": "ZZ-99", "shape_b": "YY-99",
           "measured_at": "2026-10-18T00:00:00+00:00", "basis": "measured"}
    record = di.record_from_row(row)
    assert record["signature_a"] == _SIG_A                 # written back in its one form
    assert (record["shape_a"], record["shape_b"]) == ("KX-9999", "QZ-9999")
    spaced = di.record_from_row({**row, "signature_a": _SIG_SPACED})
    assert spaced["signature_a"] == _SIG_SPACED
    assert "shape_a" not in spaced and "shape_b" not in spaced


# --------------------------------------------------------------------------- #
# 3. The agent's guard is built from the same records
# --------------------------------------------------------------------------- #

_RECORD = {**_PAIR, "read_a": 40, "read_b": 37, "signature_a": _SIG_A, "signature_b": _SIG_B,
           "shape_a": "KX-9999", "shape_b": "QZ-9999",
           "measured_at": "2026-10-18T00:00:00+00:00", "basis": "measured"}
_BASE = schema_format.format_tables_desc(_DETAILS)
_NOTE = schema_format.with_distinct_identifiers(_BASE, [_RECORD])
_WRONG = "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX-0042'"
_RIGHT = "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'QZ-0042'"


def _kind(sql, context):
    out = g.check(sql, context)
    return out and out["identifier_check"]["kind"]


def test_the_guard_reads_the_records_and_not_the_note():
    context = g.context_from(_BASE, records=[_RECORD])     # no note in the schema
    assert _kind(_WRONG, context) == "wrong_form"
    assert _kind(_RIGHT, context) is None


def test_records_win_over_a_note_that_says_otherwise():
    other = schema_format.with_distinct_identifiers(
        _BASE, [{**_RECORD, "shape_a": "QZ-9999", "shape_b": "KX-9999"}])
    assert _kind(_WRONG, g.context_from(other)) is None      # the note alone: swapped forms
    assert _kind(_WRONG, g.context_from(other, records=[_RECORD])) == "wrong_form"


def test_an_empty_record_list_means_no_guard_whatever_the_note_says():
    assert g.context_from(_NOTE) is not None                 # the fallback parse
    assert g.context_from(_NOTE, records=[]) is None


def test_unusable_records_never_switch_the_note_fallback_off():
    """An agent config whose records are not all signed records (a shape-only
    shape-only record, one signature missing or invalid) is treated as one
    configured without records: the baked note guards."""
    unsigned = {k: v for k, v in _RECORD.items() if not k.startswith("signature_")}
    for records in ([unsigned], [{**_RECORD, "signature_b": "QZ-9999"}], [_RECORD, unsigned],
                    ["junk", None]):
        assert _kind(_WRONG, g.context_from(_NOTE, records=records)) == "wrong_form", records
    assert g.context_from(_BASE, records=[unsigned]) is None  # its examples are not a source
    assert g.context_from(_BASE, records=["junk", None]) is None


def test_a_record_whose_shapes_cannot_be_printed_in_the_note_still_guards():
    spaced = {k: v for k, v in _RECORD.items() if not k.startswith("shape_")}
    spaced["signature_a"] = _SIG_SPACED
    context = g.context_from(_BASE, records=[spaced])
    out = g.check("SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX 0042'", context)
    assert out["identifier_check"]["kind"] == "wrong_form"
    assert "shaped like KX 9999" in out["note"]
    # A value of this shape in its own column runs.
    read = "SELECT c.lot_id FROM consumption c WHERE c.ref_code = 'KX 0042'"
    assert g.check(read, context) is None


class _Cursor:
    def __init__(self):
        self.description = None
        self._it = iter(())

    def execute(self, sql, *_a, **_k):
        if str(sql).lstrip().upper().startswith("SELECT"):
            self.description = [("qty",)]
            self._it = iter([(3,)])

    def fetchone(self):
        return next(self._it, None)


class _Conn:
    def set_session(self, **_k):
        return None

    def cursor(self):
        return _Cursor()

    def close(self):
        return None


def _tool(monkeypatch, desc, **kw):
    connects: list = []
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **c: connects.append(c) or _Conn())
    tools, _d = sa._system_tools(SID, SCHEMA, desc, {"host": "h"}, "PostgreSQL", None,
                                 concept_items=[], **kw)
    return tools[0], connects


def _call(tool, sql):
    return json.loads(tool._tool_func(sql=sql))


def test_the_query_tool_guards_with_the_records_it_is_given(monkeypatch):
    tool, connects = _tool(monkeypatch, _BASE, identifier_records=[_RECORD])
    assert _call(tool, _WRONG)["identifier_check"]["kind"] == "wrong_form"
    assert _call(tool, _RIGHT)["rows"] == [[3]]
    assert len(connects) == 1


def test_a_tool_given_no_records_reads_the_note_and_says_so(monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger=sa.logger.name):
        tool, connects = _tool(monkeypatch, _NOTE)
    assert _call(tool, _WRONG)["identifier_check"]["kind"] == "wrong_form"
    assert connects == []
    assert "no graph record" in caplog.text


def test_a_tool_given_unusable_records_reads_the_note_and_says_so(monkeypatch, caplog):
    import logging
    unsigned = {k: v for k, v in _RECORD.items() if not k.startswith("signature_")}
    with caplog.at_level(logging.INFO, logger=sa.logger.name):
        tool, connects = _tool(monkeypatch, _NOTE, identifier_records=[unsigned])
    assert _call(tool, _WRONG)["identifier_check"]["kind"] == "wrong_form"
    assert connects == []
    assert "not usable" in caplog.text


class _FakeAgent:
    def __init__(self, **kwargs):
        self.tools = kwargs.get("tools") or []
        self.system_prompt = kwargs.get("system_prompt", "")


def test_the_agent_takes_the_records_out_of_its_connection_config(monkeypatch):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr(sa, "_system_agent_model", lambda: None)
    connects: list = []
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **c: connects.append(c) or _Conn())
    cfg = {"distinct_identifiers": [dict(_RECORD)]}
    agent = sa.create_system_agent(
        system_id=SID, system_name="System A", schema=SCHEMA, system_prompt="p",
        tables_desc=_BASE, conn_params={"host": "h", "dbname": "d", "user": "u",
                                        "password": "p"},
        connection_type="PostgreSQL", connection_config=cfg)
    assert "distinct_identifiers" not in cfg
    (tool,) = [t for t in agent.tools if getattr(t, "_tool_func", None)]
    assert _call(tool, _WRONG)["identifier_check"]["kind"] == "wrong_form"
    assert connects == []
    assert "Same-named columns" not in agent.system_prompt   # nothing added to the prompt


# --------------------------------------------------------------------------- #
# The records reach the agent the way its other provision keys do
# --------------------------------------------------------------------------- #

class _RecordingAgentManager:
    _agents: dict = {}

    def __init__(self):
        self.kwargs: dict = {}

    def start_agent(self, **kwargs):
        self.kwargs = kwargs
        return "mcp://test-runtime/invoke"


def _start(monkeypatch, records):
    am = _RecordingAgentManager()
    monkeypatch.setattr(onboard, "_agent_manager", am)
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    ctx = _ctx(distinct_identifier_records=records)
    onboard._start_pg_agent_with(ctx, "prompt", _NOTE, {"host": "h"}, "")
    return am.kwargs["connection_config"]


def test_the_agent_config_carries_the_graph_records(monkeypatch):
    cfg = _start(monkeypatch, [dict(_RECORD)])
    assert cfg["distinct_identifiers"] == [_RECORD]
    assert cfg["concept_mappings"] == _CM


def test_no_record_adds_no_key(monkeypatch):
    assert "distinct_identifiers" not in _start(monkeypatch, [])


def test_a_resync_keeps_the_records_with_the_schema_they_belong_to():
    from agents.agent_manager import AgentManager
    current = {"host": "db.example.com", "dbname": "d", "tables_desc": _NOTE,
               "concept_mappings": _CM, "distinct_identifiers": [_RECORD]}

    class _Secrets:
        def get_secret_value(self, SecretId):
            return {"SecretString": json.dumps(current)}

    boto = types.SimpleNamespace(client=lambda *_a, **_k: _Secrets())
    with patch("agents.agent_manager.boto3", boto):
        out = AgentManager(mode="cloud")._carry_forward_generated_keys(
            SID, "PostgreSQL", {"host": "db.example.com", "dbname": "d"}, "us-east-1")
    assert out["distinct_identifiers"] == [_RECORD] and out["tables_desc"] == _NOTE


# --------------------------------------------------------------------------- #
# 4. Re-provision replaces the records; removing the system removes them
# --------------------------------------------------------------------------- #

def _stale_graph():
    """This system's Fields with a stale record, a record between two Fields
    no longer under a SchemaEntry (a Field delete through the schema misses
    those), and another system's record."""
    graph = _graph(*_DETAILS)
    graph.add_edge(f"{SID}.lots.lot_code", f"{SID}.consumption.ref_code", concept="old",
                   read_a=9, read_b=9, basis="measured")
    loose_a = graph.add_field(SID, "gone_a", "ref_code", attached=False)
    loose_b = graph.add_field(SID, "gone_b", "ref_code", attached=False)
    graph.add_edge(loose_a, loose_b, concept="unit", basis="measured")
    graph.add_field("sys-b", "t1", "c")
    graph.add_field("sys-b", "t2", "c")
    graph.add_edge("sys-b.t1.c", "sys-b.t2.c", concept="unit", basis="measured")
    return graph


def test_the_provision_replaces_the_systems_records_and_no_other(monkeypatch):
    graph = _stale_graph().install(monkeypatch)
    onboard._delete_stale_distinct_identifier_records(SID)
    assert graph.edges_of(SID) == []
    assert len(graph.edges_of("sys-b")) == 1
    _ctx_, desc = _desc(monkeypatch, graph)
    assert [(e["from"], e["to"]) for e in graph.edges_of(SID)] == [(_FIELD_A, _FIELD_B)]
    assert desc.endswith(_PAIR_TEXT + ".")


def test_a_re_provision_that_names_no_pair_leaves_no_record(monkeypatch):
    graph = _stale_graph().install(monkeypatch)
    onboard._delete_stale_distinct_identifier_records(SID)
    _ctx_, desc = _desc(monkeypatch, graph, fail={"rows": [], "unavailable": True})
    assert graph.edges_of(SID) == [] and graph.records(SID) == []
    assert schema_format.DISTINCT_IDENTIFIERS_PREFIX not in desc


def test_removing_the_system_removes_its_records_and_no_other():
    graph = _stale_graph()
    for _what, cypher in onboard._RESET_GRAPH_DELETES:
        graph.write(cypher, {"sid": SID, "short": SID})
    assert graph.edges_of(SID) == []                        # the unattached one too
    assert len(graph.edges_of("sys-b")) == 1


# The full provision, with the Field nodes b2 writes.

def _provision(monkeypatch, graph, pg_rows=None):
    am = _RecordingAgentManager()
    am.get_mcp_url = lambda _sid: "mcp://test-runtime/invoke"
    graph.install(monkeypatch)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub prompt")
    monkeypatch.setattr(onboard, "pg", _fake_pg(rows=pg_rows))
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", am)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda *_a, **_k: ["unit", "material"])
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: object())
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    # No proposal-store read leaves the process (the post-pass replays accepted items).
    monkeypatch.setattr(self_learning, "_query_by_status", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "_provision_knowledge_post_pass", lambda *_a, **_k: None)
    mappings = [{"table": item["field"].split(".")[0], "field": item["field"],
                 "concept": item["concept"],
                 "mappings": [{"concept": item["concept"], "role": "identifier",
                               "confidence": 0.9}]} for item in _CM]
    body = ProvisionRequest(system_id=SID, system_name="System A", schema=SCHEMA,
                            connection_type="PostgreSQL", connection_config={},
                            mappings=mappings, rules=[])
    result = onboard._do_provision(body)
    return result, am.kwargs


def test_a_provision_writes_the_record_between_the_fields_it_wrote(monkeypatch):
    graph = FieldGraph()                                    # no Field until b2 writes it
    result, kwargs = _provision(monkeypatch, graph)
    assert kwargs, result
    assert [(e["from"], e["to"]) for e in graph.edges_of(SID)] == [(_FIELD_A, _FIELD_B)]
    assert kwargs["tables_desc"].endswith(_PAIR_TEXT + ".")
    assert [r["column"] for r in kwargs["connection_config"]["distinct_identifiers"]] == [
        "ref_code"]
    assert result["distinct_identifier_columns"][0]["read_a"] == 40


def test_a_provision_clears_the_old_records_before_it_writes_any(monkeypatch):
    graph = _stale_graph()
    calls: list = []
    real = graph.write

    def write(cypher, params=None):
        calls.append(cypher)
        return real(cypher, params)
    graph.write = write
    _result, kwargs = _provision(monkeypatch, graph)
    assert kwargs
    deletes = [i for i, c in enumerate(calls)
               if EDGE in c and c.rstrip().endswith("DELETE r") and "DETACH" not in c]
    assert len(deletes) == 1
    schema_writes = [i for i, c in enumerate(calls) if c.startswith("MERGE (se:SchemaEntry")]
    creates = [i for i, c in enumerate(calls) if EDGE in c and "CREATE" in c]
    assert creates and deletes[0] < min(schema_writes + creates)
    assert [(e["from"], e["to"]) for e in graph.edges_of(SID)] == [(_FIELD_A, _FIELD_B)]
    assert len(graph.edges_of("sys-b")) == 1


# --------------------------------------------------------------------------- #
# The test graph runs each statement as written (it adds no scoping of its own)
# --------------------------------------------------------------------------- #

def _two_systems():
    """This system's pair and another system's pair on the same table and
    column names."""
    graph = _graph(*_DETAILS)
    for table, columns in _DETAILS.items():
        for column, _type in columns:
            graph.add_field("sys-b", table, column)
    stored = {"concept": "unit", "signature_a": _SIG_A, "signature_b": _SIG_B,
              "measured_at": "2026-10-18T00:00:00+00:00", "basis": "measured"}
    graph.add_edge(_FIELD_A, _FIELD_B, read_a=40, read_b=37, **stored)
    graph.add_edge("sys-b.consumption.ref_code", "sys-b.dispatch_lines.ref_code",
                   read_a=9, read_b=9, **stored)
    return graph


def test_the_test_graph_reads_only_what_the_read_scopes():
    graph = _two_systems()
    scoped = graph.query(onboard.DISTINCT_IDENTIFIER_RECORDS_READ, {"sid": SID})
    assert [row["read_a"] for row in scoped] == [40]
    unscoped = (onboard.DISTINCT_IDENTIFIER_RECORDS_READ
                .replace("(s:System {id: $sid})-[:HAS_SCHEMA]->", "")
                .replace("<-[:HAS_SCHEMA]-(s)", ""))
    assert unscoped != onboard.DISTINCT_IDENTIFIER_RECORDS_READ
    # Without the System path the read reaches the other system's pair too.
    assert sorted(row["read_a"] for row in graph.query(unscoped, {"sid": SID})) == [9, 40]


def test_the_test_graph_deletes_only_what_the_delete_scopes():
    graph = _two_systems()
    graph.write(onboard._DISTINCT_IDENTIFIER_RECORDS_DELETE, {"sid": SID})
    assert graph.edges_of(SID) == [] and len(graph.edges_of("sys-b")) == 1
    graph = _two_systems()
    graph.write(f"MATCH ()-[r:{EDGE}]->() DELETE r", {"sid": SID})
    assert graph.edges == []                 # an unscoped delete reaches every system


def test_the_test_graph_writes_only_where_the_write_reaches():
    graph = _graph(*_DETAILS, attached=False)   # Fields that are not under the schema
    params = {"sid": SID, "field_a": _FIELD_A, "field_b": _FIELD_B, "concept": "unit",
              "read_a": 40, "read_b": 37, "measured_at": "2026-10-18T00:00:00+00:00",
              "basis": "measured"}
    graph.write(onboard._DISTINCT_IDENTIFIER_WRITE, params)
    assert graph.edges == []                 # the write reaches a Field only through the schema
    graph.write(f"MATCH (fa:Field {{id: $field_a}}) MATCH (fb:Field {{id: $field_b}}) "
                f"CREATE (fa)-[r:{EDGE}]->(fb) SET r.concept = $concept", params)
    assert [(e["from"], e["to"]) for e in graph.edges] == [(_FIELD_A, _FIELD_B)]


@pytest.mark.parametrize("cypher", [
    f"MATCH (f:Field)-[r:{EDGE}]->() WHERE f.system_id = $sid DELETE r",
    f"MATCH (a:Field {{id: $field_a}}) MERGE (a)-[r:{EDGE}]->(a)",
    f"MATCH (f:Field)-[r:{EDGE}]->() RETURN count(r) AS n",
])
def test_the_test_graph_refuses_a_statement_it_cannot_run(cypher):
    graph = _two_systems()
    with pytest.raises(AssertionError):
        graph.write(cypher, {"sid": SID, "field_a": _FIELD_A})
    with pytest.raises(AssertionError):
        graph.query(cypher, {"sid": SID, "field_a": _FIELD_A})


# --------------------------------------------------------------------------- #
# The graph's own description of the edge, and no dataset words anywhere
# --------------------------------------------------------------------------- #

def test_the_metamodel_documents_the_edge_without_a_rule():
    (entry,) = [e for e in metamodel.META_EDGES if e["name"] == EDGE]
    assert (entry["from_label"], entry["to_label"]) == ("Field", "Field")
    assert entry["guidance"] == ""
    text = entry["description"]
    for prop in ("concept", "read_a", "read_b", "signature_a", "signature_b", "measured_at",
                 "basis"):
        assert prop in text, prop
    assert "shape_a" not in text and "shape_b" not in text   # not stored on the edge
    assert not re.search(r"\b(never|must|do not|don't|always|should)\b", text, re.IGNORECASE)


_DATASET_WORDS = ("batch", "shipment", "lot_mes", "work_order", "work order", "erp", "mes",
                  "run_number")


def test_the_graph_statements_and_the_edge_name_no_dataset(monkeypatch):
    graph = _graph(*_DETAILS)
    _desc(monkeypatch, graph)
    graph.install(monkeypatch)
    onboard._delete_stale_distinct_identifier_records(SID)
    (entry,) = [e for e in metamodel.META_EDGES if e["name"] == EDGE]
    texts = [c for _k, c, _p in graph.statements] + [entry["description"]]
    texts += [c for _w, c in onboard._RESET_GRAPH_DELETES if EDGE in c]
    for text in texts:
        for word in _DATASET_WORDS:
            assert not re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE), (word, text)
