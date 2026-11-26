# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The agent test proves a Tulip read from its call's trace.

Before: these sources write no SQL statement, so the agent test read only the
answer text. A digit or "six records" was the expected ``no_read`` (shown as a
pass), and "six" failed with "returned no row count", although the tool read
the source either way. Nothing tied the result to a read.

Now each connector declares which of its read tools reads which entity
(``system_agent.entity_reads_for``), and the test passes when the trace the
system agent wrote for this call shows a successful call of a read tool
mapped to the tested entity whose tool parsed the source's response
(``records_read``, zero for an empty entity). A Tulip table is read only when
the call's ``table_id`` is that table's id. SiteWise Direct and i3X reads are
never proven this way: a source-named table there can take a connector-fixed
table's name, and no table origin is kept, so the connector declares that a
name does not tell the two apart (``names_tell_origin``) and every table of
such a system keeps the base outcome, trace or not. The answer text never
proves the read. The tested table is the first by name, as on the base, and with no
proving call every outcome is the base's (pinned against literals taken from
origin/feat/v2 below). Every read the proof adds answers by the deadline.

Entity names and ids are made up and differ from the seed data. Offline.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import agents.investigation_trace_store as its
from agents import connection_types as ct
from agents.job_store import JobStore
from tests.unit._shared_table import SharedTable
from tests.unit.test_onboard_verify_agent_test import (  # noqa: F401 - fixtures
    MODE_A_SCHEMA, SID, FakeClock, FakeControl, FakeGateway, _mod, _ok, _runtime, _target,
    env, real_clock)


def _step(tool, args=None, *, status="success", records_read=4):
    step = {"seq": 1, "tool_name": tool, "tool_input": json.dumps(args or {}),
            "status": status}
    if records_read is not None:
        step["records_read"] = records_read
    return step


class TracingGateway(FakeGateway):
    """The gateway whose system runtime writes its investigation trace for the
    call (``steps``; ``None`` writes none) under the call's provenance
    context, through ``writer`` (the runtime's own store) when given."""

    def __init__(self, tool, answer, steps, writer=None):
        super().__init__(tools=[{"name": tool, "input_schema": MODE_A_SCHEMA}],
                         replies=[_ok(answer)], capture=False)
        self.steps, self.writer = steps, writer

    def call_tool(self, name, arguments, timeout_s, tool_use_id=None):
        from agents.provenance import parse_provenance_context
        context = parse_provenance_context(arguments.get("_adt_provenance"))
        if context is not None and self.steps is not None:
            reader = its._job_store
            its._job_store = self.writer or reader
            try:
                its.put_trace(*context, {"system_id": SID, "question": arguments["question"],
                                         "steps": self.steps, "final_answer": "answered"})
            finally:
                its._job_store = reader
        return super().call_tool(name, arguments, timeout_s, tool_use_id)


@pytest.fixture
def traces(env, monkeypatch):
    monkeypatch.setattr(its, "_job_store", JobStore(kind="investigation-trace", table=None))
    return env


def _call(env, ctype, tables, answer, steps, *, writer=None, deadline_s=None,
          gateway=None):
    """``(result, gateway)`` of one agent test of a ``ctype`` source. A
    ``deadline_s`` runs it on the real clock; else the clock is faked."""
    mod = _mod()
    env["entry"] = {"id": SID, "connection_type": ctype, "schema": "src"}
    env["tables"] = [{"name": name} for name in tables]
    gateway = gateway or TracingGateway(mod._tool_names(SID)[0], answer, steps, writer)
    timing = ({"deadline_s": deadline_s} if deadline_s is not None
              else {"clock": (clock := FakeClock()), "sleep": clock.sleep})
    result = mod.run_agent_test(
        SID, user_access_token="user-token",
        control=FakeControl([[_runtime(name=mod.runtime_name(SID))]],
                            [[_target(name=mod.target_name(SID))]]),
        open_session=gateway.open, **timing)
    assert gateway.calls, result
    return result, gateway


def _run(env, ctype, tables, answer, steps, *, writer=None):
    return _call(env, ctype, tables, answer, steps, writer=writer)[0]


def _shown(result) -> tuple:
    """Outcome, step and reason, without the reason's random log reference."""
    return (result["outcome"], result["step"],
            str(result["reason"] or "").split("Reference:", 1)[0])


def _untraced(env, ctype, tables, answer):
    """This branch's outcome for the answer with no trace at all (which the
    base-literal test below pins to the base's)."""
    return _shown(_run(env, ctype, tables, answer, None))


CMMS = ["WorkOrder", "Problem", "Asset", "part-usage"]
#: The base's reasons for an API source's answer with no proof (as below).
_NO_READ_TEXT = ("query_system_a answered through the gateway. This type of system "
                 "doesn't record which table a read used, so the test can't confirm it "
                 "read {table}. That's expected here.")
_NO_COUNT_TEXT = "query_system_a returned no row count for {table}. "


# ── Tulip: the call must read the tested table by its id ─────────────────────

TULIP_TABLES = ["Shift Log", "Kiln Batches"]


@pytest.fixture
def tulip_ids(traces, monkeypatch):
    """The Tulip table-id lookup, faked: label -> id, and every name asked."""
    asked: list = []
    ids = {"Kiln Batches": "kb7Q2", "Shift Log": "sl9"}

    def _lookup(system_id, table):
        asked.append((system_id, table))
        return ids.get(table, "")
    monkeypatch.setattr(_mod(), "_tulip_table_id", _lookup)
    return ids, asked


def test_a_tulip_read_of_the_tested_tables_id_passes(traces, tulip_ids):
    _ids, asked = tulip_ids
    result = _run(traces, ct.TULIP, TULIP_TABLES, "six",
                  [_step("tulip_query_records", {"table_id": "kb7Q2", "limit": 100})])
    assert (result["outcome"], result["table"]) == ("passed", "Kiln Batches"), result
    assert asked == [(SID, "Kiln Batches")]


@pytest.mark.parametrize("steps", [
    [_step("tulip_query_records", {"table_id": "sl9"})],
    [_step("tulip_query_records", {"limit": 20})],
    [_step("tulip_get_table_columns", {"table_id": "kb7Q2"})],
    [_step("tulip_query_records", {"table_id": "kb7Q2"}, records_read=None)],
    [_step("tulip_query_records", {"table_id": "sl9"}, records_read=0)],
], ids=["other-table", "no-id", "columns-only", "no-count", "other-table-empty"])
def test_a_tulip_call_that_did_not_read_the_tested_table_changes_nothing(
        traces, tulip_ids, steps):
    result = _run(traces, ct.TULIP, TULIP_TABLES, "6", steps)
    assert _shown(result) == _untraced(
        traces, ct.TULIP, TULIP_TABLES, "6")


def test_an_empty_tulip_table_read_by_its_id_passes(traces, tulip_ids):
    result = _run(traces, ct.TULIP, TULIP_TABLES, "0",
                  [_step("tulip_query_records", {"table_id": "kb7Q2"}, records_read=0)])
    assert (result["outcome"], result["table"], result["rows"]) == (
        "passed", "Kiln Batches", 0), result


def test_the_tulip_lookup_runs_only_for_a_call_that_read_records(traces, tulip_ids):
    _ids, asked = tulip_ids
    _run(traces, ct.TULIP, TULIP_TABLES, "6", [_step("tulip_list_tables")])
    _run(traces, ct.TULIP, TULIP_TABLES, "6", None)
    assert asked == []


@pytest.mark.parametrize("lookup", [lambda *_a: "", lambda *_a: None],
                         ids=["unresolved", "none"])
def test_a_tulip_table_whose_id_cannot_be_found_is_not_proven(traces, monkeypatch, lookup):
    monkeypatch.setattr(_mod(), "_tulip_table_id", lookup)
    result = _run(traces, ct.TULIP, TULIP_TABLES, "6",
                  [_step("tulip_query_records", {"table_id": "kb7Q2"})])
    assert result["outcome"] == "no_read", result


def test_a_tulip_lookup_that_fails_is_not_proven(traces, monkeypatch):
    def _boom(*_a):
        raise RuntimeError("session store unreachable")
    monkeypatch.setattr(_mod(), "_tulip_table_id", _boom)
    result = _run(traces, ct.TULIP, TULIP_TABLES, "6",
                  [_step("tulip_query_records", {"table_id": "kb7Q2"})])
    assert result["outcome"] == "no_read", result


# ── SiteWise and i3X: every table keeps the base outcome ───────────────────
# Both connectors have tables fixed by the connector (a hierarchy table, the
# fallback tool tables, i3X's Asset, AssetModel and Property) and tables the
# source names (asset models, object types), and discovery lets the source
# name a table after a fixed one. No table origin is kept, so a trace never
# proves a read of any table of these systems: the answer decides, as on the
# base.

import agents.system_agent as sa  # noqa: E402
from tests.unit.test_entity_reads import _ACCEPTS  # noqa: E402

_ORIGIN_UNTOLD = {ct.SITEWISE_DIRECT: ("sitewise-direct", "Press Line Model", "asset_model_id"),
                  ct.SITEWISE_I3X: ("i3x", "Furnace", "type_element_id")}
EVERY_TABLE = [(ctype, name) for ctype, (_c, model, _a) in _ORIGIN_UNTOLD.items()
               for name in sorted(sa.entity_reads_for(ctype).named) + [model]]
#: The fixed names each discovery also accepts from the source as a table name
#: (pinned in test_entity_reads.py).
COLLISIONS = [(ct.SITEWISE_DIRECT, name) for name in
              sorted(sa.entity_reads_for(ct.SITEWISE_DIRECT).named)] + [
    (ct.SITEWISE_I3X, name) for name in ("Asset", "AssetModel", "Property")]
_BASE_ANSWER = [("6", "no_read", None, _NO_READ_TEXT), ("six", "failed", "tool", _NO_COUNT_TEXT)]


def _would_prove(ctype, table, count=4) -> list:
    """Successful traced reads that would prove ``table`` under any rule of
    its connector: every tool of the rule its name gets, with no id, and the
    source-named rule's tool with an id."""
    reads = sa.entity_reads_for(ctype)
    arg = _ORIGIN_UNTOLD[ctype][2]
    return ([_step(tool, {}, records_read=count) for tool in reads.rule(table).tools]
            + [_step(reads.source_named.tools[0], {arg: "id-any"}, records_read=count)])


def _base_outcome(traces, ctype, table, steps, answer, outcome, step, reason):
    result = _run(traces, ctype, [table], answer, steps)
    assert (result["table"], result["read_provable"], result["rows"]) == (table, False, None)
    assert _shown(result) == (outcome, step, reason.format(table=table)), result


@pytest.mark.parametrize("ctype, table", EVERY_TABLE,
                         ids=[f"{_ORIGIN_UNTOLD[c][0]}-{n}" for c, n in EVERY_TABLE])
@pytest.mark.parametrize("count", [4, 0], ids=["records", "empty"])
@pytest.mark.parametrize("answer, outcome, step, reason", _BASE_ANSWER,
                         ids=["digit", "bare-word"])
def test_every_table_of_a_sitewise_or_i3x_system_keeps_the_base_outcome(
        traces, ctype, table, count, answer, outcome, step, reason):
    _base_outcome(traces, ctype, table, _would_prove(ctype, table, count), answer, outcome,
                  step, reason)


@pytest.mark.parametrize("ctype, table", COLLISIONS,
                         ids=[f"{_ORIGIN_UNTOLD[c][0]}-{n}" for c, n in COLLISIONS])
@pytest.mark.parametrize("answer, outcome, step, reason", _BASE_ANSWER,
                         ids=["digit", "bare-word"])
def test_a_source_named_table_with_a_fixed_name_keeps_the_base_outcome(
        traces, ctype, table, answer, outcome, step, reason, monkeypatch):
    """A source model or type that discovery accepts under a fixed rule's
    name: a successful traced call of the fixed rule's tool, with no id,
    proves nothing, as on the base."""
    assert _ACCEPTS[_ORIGIN_UNTOLD[ctype][0]](table, monkeypatch)
    fixed = sa.entity_reads_for(ctype).named[table]
    _base_outcome(traces, ctype, table, [_step(tool, {}) for tool in fixed.tools], answer,
                  outcome, step, reason)


class _ChatSession:
    """An onboarding session whose saved tables carry ``tables``."""

    def __init__(self, tables):
        self.tables = tables

    def get_session(self, system_id, **_read):
        return {"system_id": system_id, "discovered_tables": self.tables}


@pytest.mark.parametrize("ctype, tables, arg, model_id", [
    (ct.SITEWISE_DIRECT, ["Press Line Model", "asset_hierarchy"], "asset_model_id", "m-press"),
    (ct.SITEWISE_I3X, ["Furnace", "Hierarchy"], "type_element_id", "furnace-type"),
], ids=["sitewise-direct", "i3x"])
@pytest.mark.parametrize("count", [6, 0], ids=["records", "empty"])
@pytest.mark.parametrize("answer, outcome, step, reason", [
    ("6", "no_read", None, _NO_READ_TEXT),
    ("six", "failed", "tool", _NO_COUNT_TEXT),
], ids=["digit", "bare-word"])
def test_a_model_table_tested_first_keeps_the_base_outcome_with_a_trace(
        traces, ctype, tables, arg, model_id, count, answer, outcome, step, reason,
        monkeypatch):
    """Limit, pinned: the app keeps no SiteWise model or i3X type id, so even
    a successful traced list of that model's assets by its own id (records or
    an empty answer) cannot be tied to the tested model. The outcome is the
    base's for the table the base tests: the answer decides. A saved session
    whose tables happen to carry the id changes nothing: nothing reads it."""
    import agents.onboard_agent as oa
    session = _ChatSession([{"name": tables[0], "columns": [], "source_id": model_id}])
    monkeypatch.setattr(oa, "_store", lambda: session)
    result = _run(traces, ctype, tables, answer,
                  [_step("sitewise_list_assets", {arg: model_id}, records_read=count)])
    assert (result["table"], result["read_provable"], result["rows"]) == (tables[0], False,
                                                                           None)
    assert _shown(result) == (outcome, step, reason.format(table=tables[0]))


# ── with no proof, every connector answers as origin/feat/v2 does ────────────
# Literals taken from the base module (origin/feat/v2 1406b1ca), checked by
# running this test with that module loaded in place of this branch's
# onboard_verify_agent. The question, the tested table and every result
# field but the time taken and the random log reference.

_QUESTION = ('How many records does "{table}" hold in this system? Count them with as '
             "few reads as you can and reply with only the number.")
_NO_READ, _NO_COUNT = _NO_READ_TEXT, _NO_COUNT_TEXT
#: ``{connector: (type, the graph's tables, the table tested, a read of it)}``.
BASE_CONNECTORS = {
    "tulip": (ct.TULIP, ["Shift Log", "Kiln Batches"], "Kiln Batches",
              ("tulip_query_records", {"table_id": "kb7Q2"})),
    "sitewise-direct": (ct.SITEWISE_DIRECT,
                        ["Press Line Model", "Curing Oven", "asset_hierarchy"], "Curing Oven",
                        ("sitewise_list_assets", {"asset_model_id": "m-oven"})),
    "i3x": (ct.SITEWISE_I3X, ["Furnace", "Hierarchy", "Namespace"], "Furnace",
            ("sitewise_list_assets", {"type_element_id": "furnace-type"})),
    "rest": (ct.REST_API, ["orders", "lines"], "lines", ("rest_get_orders", {})),
}
#: ``{answer kind: (answer, outcome, step, reason)}``.
BASE_ANSWERS = {
    "digit": ("6", "no_read", None, _NO_READ),
    "count-words": ("There are six records in it.", "no_read", None, _NO_READ),
    "bare-word": ("six", "failed", "tool", _NO_COUNT),
    "no-count": ("I can describe it.", "failed", "tool", _NO_COUNT),
    "error": ("Error: the source answered with HTTP 503.", "failed", "tool", _NO_COUNT),
}
#: Traces that prove nothing. ``unprovable-read`` is a successful read of the
#: tested table with no way to tie it to the table (a SiteWise model or i3X
#: type, whose id the app does not keep, or a type with no rules); for the
#: others it would be a proof, so it is left out.
_UNPROVABLE = ("sitewise-direct", "i3x", "rest")
_BASE_CASES = [(c, a, t) for c in sorted(BASE_CONNECTORS) for a in sorted(BASE_ANSWERS)
               for t in ("none", "failed-call", "no-count", "unprovable-read")
               if t != "unprovable-read" or c in _UNPROVABLE]


def _no_proof_trace(kind, tool, args):
    return {"none": None,
            "failed-call": [_step(tool, args, status="error")],
            "no-count": [_step(tool, args, records_read=None)],
            "unprovable-read": [_step(tool, args, records_read=4)]}[kind]


@pytest.mark.parametrize("connector, answer, trace", _BASE_CASES,
                         ids=[f"{c}-{a}-{t}" for c, a, t in _BASE_CASES])
def test_with_no_proof_every_connector_answers_as_the_base_does(traces, connector, answer,
                                                                  trace):
    ctype, tables, table, (tool, args) = BASE_CONNECTORS[connector]
    text, outcome, step, reason = BASE_ANSWERS[answer]
    result, gateway = _call(traces, ctype, tables, text,
                            _no_proof_trace(trace, tool, args))
    shown = {key: result[key] for key in ("outcome", "step", "system_id", "tool", "table",
                                          "rows", "query_kind", "read_provable",
                                          "deadline_s")}
    shown["reason"] = str(result["reason"]).split("Reference:", 1)[0]
    assert shown == {"outcome": outcome, "step": step, "reason": reason.format(table=table),
                     "system_id": SID, "tool": "system-a___query_system_a", "table": table,
                     "rows": None, "query_kind": "question", "read_provable": False,
                     "deadline_s": 180}
    assert gateway.calls[0][1]["question"] == _QUESTION.format(table=table)


# ── every read the proof adds answers by the deadline ───────────────────────

def _blocking(release, value=None):
    def _wait(*_a, **_k):
        release.wait(5.0)
        return value
    return _wait


def _warm(traces, ctype, tables, steps):
    """One run on the fake clock first, so every module the proof imports
    lazily is imported before the real clock starts."""
    _run(traces, ctype, tables, "6", steps)


def test_a_blocking_capture_read_returns_by_the_deadline(traces, monkeypatch, real_clock):
    """PostgreSQL: the capture read is a stage like the others, so a store
    that does not answer fails the test at the deadline, naming the read."""
    from agents import provenance_store
    release = threading.Event()
    monkeypatch.setattr(provenance_store, "get_query", _blocking(release))
    gateway = FakeGateway(capture=False)
    started = time.monotonic()
    try:
        result, _ = _call(traces, ct.POSTGRESQL, ["table_x"], "12", None, deadline_s=0.3,
                          gateway=gateway)
    finally:
        release.set()
    assert time.monotonic() - started < 1.5
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert result["reason"] == ("the agent test ran out of time (0.3 s) while reading the "
                                "call's provenance capture"), result


def test_a_blocking_tulip_id_lookup_returns_by_the_deadline(traces, monkeypatch, real_clock):
    steps = [_step("tulip_query_records", {"table_id": "kb7Q2"})]
    release = threading.Event()
    monkeypatch.setattr(_mod(), "_tulip_table_id", lambda *_a: "kb7Q2")
    _warm(traces, ct.TULIP, TULIP_TABLES, steps)
    monkeypatch.setattr(_mod(), "_tulip_table_id", _blocking(release, "kb7Q2"))
    started = time.monotonic()
    try:
        result, _ = _call(traces, ct.TULIP, TULIP_TABLES, "6", steps, deadline_s=0.3)
    finally:
        release.set()
    assert time.monotonic() - started < 1.5
    assert result["outcome"] == "no_read" and "That's expected here." in result["reason"], \
        result


# ── what is unchanged ────────────────────────────────────────────────────────

def test_a_trace_never_stands_in_for_a_postgresql_capture(traces):
    result = _run(traces, ct.POSTGRESQL, ["table_x"], "12",
                  [_step("query_src", {"sql": "SELECT 1"}, records_read=12)])
    assert result["outcome"] == "no_read" and "recorded no read" in result["reason"], result


def test_the_built_in_hub_is_unchanged(traces):
    """Its tables are its tools and it declares no entity reads: a trace
    changes nothing."""
    from agents.builtin_highbyte import BUILTIN_ID
    mod = _mod()
    traces["entry"] = {"id": BUILTIN_ID, "connection_type": ct.MCP_SERVER, "schema": "mcp"}
    traces["tables"] = [{"name": "Tool_A"}]
    tool = mod._tool_names(BUILTIN_ID)[0]
    gateway = TracingGateway(tool, "5", [_step("Tool_A")])
    clock = FakeClock()
    result = mod.run_agent_test(
        BUILTIN_ID, user_access_token="user-token",
        control=FakeControl([[_runtime(name=mod.runtime_name(BUILTIN_ID))]],
                            [[_target(name=mod.target_name(BUILTIN_ID))]]),
        open_session=gateway.open, clock=clock, sleep=clock.sleep)
    assert result["outcome"] == "no_read", result


