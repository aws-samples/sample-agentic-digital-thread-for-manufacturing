# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The map step and the provision step read a mapping's field the same way.

An MCP namespace answers one table whose columns are dotted tag paths
(``AreaA.Line1.Line1_Output``) plus metadata columns (``_timestamp``). The map
model names a column either with the table in front
(``NameSpace_MCP.AreaA.Line1.Line1_Output``) or without it
(``AreaA.Line1.Line1_Output``), and which one it picks varies per run. The map
step joins both to the discovered column. Provision has to accept what the
map step accepted, and must still refuse a field that names no column
discover recorded:

* the map step writes every mapping it keeps as ``<table>.<column>``, with the
  column discover recorded, so the wizard sends provision one form;
* provision resolves a field against the columns discover recorded: the
  remainder after ``<table>.``, or the whole field when that is a column, else
  the qualified-identifier reading (``db.table.column`` keeps ``column``).

The end-to-end tests run the wizard's discover, map job and provision, and the
chat's provision, on the fakes of test_chat_provision_is_the_wizard_provision.py;
the map join and the provision check are the real code.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest

import agents.onboard as onboard
import agents.onboard_agent as oa
from agents.job_store import JobStore
from agents.models import DiscoverRequest, MapRequest, ProvisionRequest
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    JOB_WAIT, SID, World, _chat_provision_answer, _chat_until_resolved)

TABLE = "NameSpace_MCP"
#: Discovered columns and their types: multi-dot tag paths and one metadata column.
COLUMNS = {
    "AreaA.Line1.Line1_Output": "int",
    "AreaA.Line1.Line1_MaterialID": "str",
    "AreaA.Line2.Line2Rate.Line2Rate_Performance": "float",
    "AreaA.Line2.Line2_Running": "bool",
    "AreaB.Line3.Line3_Speed": "float",
    "_timestamp": "str",
}
NAMESPACE = [{"name": TABLE, "row_count": len(COLUMNS),
              "columns": [{"name": c, "data_type": t} for c, t in COLUMNS.items()]}]
FULL = {f"{TABLE}.{c}" for c in COLUMNS}


def _written_as(column: str, i: int) -> str:
    """The field the map model writes for ``column``: the metadata column and
    every other tag path with the table in front, the rest without it."""
    return f"{TABLE}.{column}" if column.startswith("_") or i % 2 else column


def _model_rows(columns) -> list[dict]:
    return [{"table": TABLE, "field": _written_as(c, i),
             "mappings": [{"concept": "sensor-reading", "role": "measure", "confidence": 0.9}],
             "concept": "sensor-reading", "reasoning": "tag path"}
            for i, c in enumerate(columns)]


def _both_forms() -> list[dict]:
    rows = _model_rows(COLUMNS)
    forms = {r["field"].startswith(TABLE + ".") for r in rows}
    assert forms == {True, False}, "the fixture must carry both forms"
    return rows


def _stamped(rows: list[dict], revision: str) -> list[dict]:
    """Rows as a map result carries them to provision: each with the type,
    relation kind and schema revision of its column."""
    out = []
    for r in copy.deepcopy(rows):
        column = r["field"][len(TABLE) + 1:] if r["field"].startswith(TABLE + ".") else r["field"]
        out.append({**r, "data_type": COLUMNS[column], "relation_kind": "unknown",
                    "schema_revision": revision})
    return out


@pytest.fixture
def provenance(monkeypatch):
    """A fresh in-memory provenance store; returns ``store(system_id, tables) -> revision``."""
    monkeypatch.setattr(onboard, "_schema_provenance", JobStore("schema-provenance", table=None))

    def _store(system_id: str, tables: list) -> str:
        answered = onboard._store_discovered_provenance(
            system_id, {"tables": copy.deepcopy(tables)})
        return answered["schema_revision"]
    return _store


def _body(system_id: str, mappings: list, connection_type: str = "MCP Server") -> ProvisionRequest:
    return ProvisionRequest(system_id=system_id, system_name="System", plant="Plant A",
                            schema="", rules=[], mappings=mappings,
                            connection_type=connection_type,
                            connection_string="https://mcp.example.com/mcp")


# ── the map step writes one form ───────────────────────────────────────────

def test_the_map_writes_each_mapping_it_keeps_as_table_dot_column():
    result = onboard._attach_map_provenance(
        {"mappings": _both_forms()}, onboard._mapping_schema_metadata(NAMESPACE))
    rows = result["mappings"]
    assert {r["field"] for r in rows} == FULL
    assert all(r["table"] == TABLE for r in rows)
    assert {r["field"]: r["data_type"] for r in rows} == {
        f"{TABLE}.{c}": t for c, t in COLUMNS.items()}


def test_the_map_still_leaves_out_a_field_that_names_no_column():
    rows = _both_forms() + [{"table": TABLE, "field": "AreaA.Line9.Line9_Missing"},
                            {"table": TABLE, "field": f"{TABLE}.Line1_Output"}]
    result = onboard._attach_map_provenance(
        {"mappings": rows}, onboard._mapping_schema_metadata(NAMESPACE))
    assert {r["field"] for r in result["mappings"]} == FULL


def test_the_chat_join_leaves_nothing_out_when_the_map_rewrites_a_field():
    """The chat's join (``_joined_mappings``) reports which rows the map step
    left out; a row the map step kept under its ``<table>.<column>`` name is
    not one of them."""
    from agents import onboard_chat_lifecycle as lifecycle
    joined, left_out = lifecycle._joined_mappings(_both_forms(), NAMESPACE)
    assert left_out == []
    assert {r["field"] for r in joined} == FULL
    extra = {"table": TABLE, "field": "AreaA.Line9.Line9_Missing"}
    joined, left_out = lifecycle._joined_mappings(_both_forms() + [extra], NAMESPACE)
    assert left_out == ["AreaA.Line9.Line9_Missing"]
    assert {r["field"] for r in joined} == FULL


# ── provision checks the field against what discover recorded ──────────────

def test_provision_takes_a_mapping_in_either_form(provenance):
    revision = provenance("ns-sys", NAMESPACE)
    trusted = onboard._trusted_provision_body(_body("ns-sys", _stamped(_both_forms(), revision)))
    assert {m["field"] for m in trusted.mappings} == FULL
    assert all(m["table"] == TABLE for m in trusted.mappings)
    assert {m["field"]: m["data_type"] for m in trusted.mappings} == {
        f"{TABLE}.{c}": t for c, t in COLUMNS.items()}
    again = onboard._trusted_provision_body(trusted)
    assert [m["field"] for m in again.mappings] == [m["field"] for m in trusted.mappings]


def test_the_steps_after_the_provision_check_read_the_whole_column(provenance):
    """Every later reader of a checked mapping (the graph writes, the agent's
    concept mappings, the relation-kind and type writes) splits it with
    ``_mapping_table_field``; a multi-dot column stays whole."""
    revision = provenance("ns-sys", NAMESPACE)
    trusted = onboard._trusted_provision_body(_body("ns-sys", _stamped(_both_forms(), revision)))
    assert {onboard._mapping_table_field(m, default_table="ns-sys") for m in trusted.mappings} == {
        (TABLE, c) for c in COLUMNS}
    items = onboard._concept_mapping_items(trusted.mappings, default_table="ns-sys",
                                           committable={"sensor-reading"})
    assert {it["field"] for it in items} == FULL


@pytest.mark.parametrize("field", [
    "AreaA.Line9.Line9_Missing",
    f"{TABLE}.AreaA.Line9.Line9_Missing",
    "Line1_Output",
    "AreaZ.Line1_Output",
    f"{TABLE}.Line1.Line1_Output",
])
def test_provision_refuses_a_field_that_names_no_discovered_column(provenance, field):
    revision = provenance("ns-sys", NAMESPACE)
    rows = _stamped(_both_forms(), revision) + [
        {"table": TABLE, "field": field, "concept": "sensor-reading",
         "schema_revision": revision}]
    with pytest.raises(ValueError):
        onboard._trusted_provision_body(_body("ns-sys", rows))


def test_provision_still_refuses_a_type_the_discovered_column_does_not_have(provenance):
    revision = provenance("ns-sys", NAMESPACE)
    rows = _stamped(_both_forms(), revision)
    unprefixed = next(r for r in rows if not r["field"].startswith(TABLE + "."))
    unprefixed["data_type"] = "str" if unprefixed["data_type"] != "str" else "int"
    with pytest.raises(ValueError):
        onboard._trusted_provision_body(_body("ns-sys", rows))


def test_a_database_qualified_field_keeps_its_last_segment(provenance):
    tables = [{"name": "example_segments", "columns": [
        {"name": "segment_id", "data_type": "string"},
        {"name": "segment_name", "data_type": "string"}]}]
    revision = provenance("ath-sys", tables)
    rows = [{"table": "example_segments",
             "field": "example_athena_db.example_segments.segment_id",
             "data_type": "string", "schema_revision": revision}]
    trusted = onboard._trusted_provision_body(_body("ath-sys", rows, "Amazon Athena"))
    assert [(m["table"], m["field"]) for m in trusted.mappings] == [
        ("example_segments", "example_segments.segment_id")]


@pytest.mark.parametrize("connection_type", ["PostgreSQL", "Amazon Athena"])
def test_a_single_dot_mapping_is_unchanged(provenance, connection_type):
    tables = [{"name": "work_orders", "columns": [
        {"name": "order_id", "data_type": "text"},
        {"name": "status", "data_type": "text"}]}]
    revision = provenance("sql-sys", tables)
    rows = [{"table": "work_orders", "field": "work_orders.order_id", "data_type": "text",
             "schema_revision": revision},
            {"table": "work_orders", "field": "status", "data_type": "text",
             "schema_revision": revision}]
    trusted = onboard._trusted_provision_body(_body("sql-sys", rows, connection_type))
    assert [m["field"] for m in trusted.mappings] == ["work_orders.order_id", "work_orders.status"]


# ── one reading, or none: a field that could name two columns is refused ───

def _tables(**columns_by_table) -> list[dict]:
    return [{"name": name, "columns": [{"name": c, "data_type": "str"} for c in columns]}
            for name, columns in columns_by_table.items()]


def _check(provenance, tables, field, table=None):
    """``(table, field)`` provision makes of one mapping row, or ValueError."""
    revision = provenance("amb-sys", tables)
    row = {"field": field, "concept": "sensor-reading", "schema_revision": revision}
    if table is not None:
        row["table"] = table
    (checked,) = onboard._trusted_provision_body(_body("amb-sys", [row])).mappings
    return checked["table"], checked["field"]


def test_a_table_less_field_two_columns_could_answer_is_refused(provenance):
    """``A.B.X`` with no table reads as table ``A`` column ``B.X`` or as a
    qualified table ``B`` column ``X``; both are discovered, so it is refused."""
    tables = [{"name": "A", "columns": [{"name": "B.X", "data_type": "str"}]},
              {"name": "B", "columns": [{"name": "X", "data_type": "str"}]}]
    with pytest.raises(ValueError):
        _check(provenance, tables, "A.B.X")
    assert _check(provenance, tables, "A.B.X", table="A") == ("A", "A.B.X")


def test_a_column_spelled_like_another_tables_column_needs_its_table(provenance):
    """Table ``A`` has a column literally named ``B.id``, table ``B`` a column
    ``id``. ``B.id`` with no table names either; with a table it names one."""
    tables = [{"name": "A", "columns": [{"name": "B.id", "data_type": "str"}]},
              {"name": "B", "columns": [{"name": "id", "data_type": "str"}]}]
    with pytest.raises(ValueError):
        _check(provenance, tables, "B.id")
    assert _check(provenance, tables, "B.id", table="A") == ("A", "A.B.id")
    assert _check(provenance, tables, "B.id", table="B") == ("B", "B.id")


def test_a_table_with_both_spellings_of_a_column_is_refused(provenance):
    """Table ``T`` has columns ``X`` and ``T.X``. ``T.X`` names both, and the
    ``<table>.<column>`` spelling of ``X`` is ``T.X`` again, so neither reads
    one way; ``T.T.X`` names only the column ``T.X``."""
    tables = [{"name": "T", "columns": [{"name": "X", "data_type": "str"},
                                        {"name": "T.X", "data_type": "str"}]}]
    for field in ("T.X", "X"):
        with pytest.raises(ValueError):
            _check(provenance, tables, field, table="T")
    assert _check(provenance, tables, "T.T.X", table="T") == ("T", "T.T.X")


@pytest.mark.parametrize("field,expected", [
    (f"{TABLE}.AreaA.Line1.Line1_Output", (TABLE, f"{TABLE}.AreaA.Line1.Line1_Output")),
    ("AreaA.Line1.Line1_Output", (TABLE, f"{TABLE}.AreaA.Line1.Line1_Output")),
    (f"{TABLE}._timestamp", (TABLE, f"{TABLE}._timestamp")),
])
def test_a_table_less_field_one_column_answers_is_taken(provenance, field, expected):
    assert _check(provenance, NAMESPACE, field) == expected


@pytest.mark.parametrize("field", ["AreaA.Line9.Line9_Missing", "Line1_Output",
                                   "Other.AreaA.Line1.Line1_Output"])
def test_a_table_less_field_no_column_answers_is_refused(provenance, field):
    with pytest.raises(ValueError):
        _check(provenance, NAMESPACE, field)


def test_a_table_less_qualified_identifier_is_taken_when_one_column_answers(provenance):
    tables = _tables(example_segments=["segment_id", "segment_name"], other=["code"])
    assert _check(provenance, tables, "example_athena_db.example_segments.segment_id") == (
        "example_segments", "example_segments.segment_id")


def test_a_qualified_identifier_with_an_explicit_table_reads_one_column(provenance):
    """With its table, ``db.t.b.c`` is the column ``b.c`` (the column after
    ``t.``); a qualifier that is not the table is read by the column the field
    ends with, and refused when two columns end it."""
    tables = _tables(t=["b.c", "c"])
    assert _check(provenance, tables, "db.t.b.c", table="t") == ("t", "t.b.c")
    assert _check(provenance, tables, "db.t.c", table="t") == ("t", "t.c")
    with pytest.raises(ValueError):
        _check(provenance, tables, "q.b.c", table="t")


def test_the_map_leaves_out_a_row_that_does_not_read_one_way():
    """The map step keeps only what provision then takes: with columns ``X``
    and ``T.X`` in ``T``, a row ``T.X`` (or ``X``) is left out, ``T.T.X`` is kept."""
    tables = _tables(T=["X", "T.X"])
    rows = [{"table": "T", "field": f} for f in ("T.X", "X", "T.T.X")]
    kept = onboard._attach_map_provenance(
        {"mappings": rows}, onboard._mapping_schema_metadata(tables))["mappings"]
    assert [(m["table"], m["field"]) for m in kept] == [("T", "T.T.X")]


# ── end to end: the wizard, and the chat ───────────────────────────────────

def _namespace_world(monkeypatch) -> World:
    world = World(monkeypatch)
    world.discover_tables = copy.deepcopy(NAMESPACE)
    return world


def _map_model(prompt, *_a, **_k) -> str:
    """The wizard's map model: a row for each namespace column listed in the
    prompt, in the form :func:`_written_as` gives it."""
    listed = [c for c in COLUMNS if f"- {c} " in prompt or f"- {c}\n" in prompt
              or prompt.endswith(f"- {c}")]
    return json.dumps(_model_rows(listed))


def _chat_map_model(prompt, *_a, **_k) -> str:
    """The chat's map_fields model: the same rows for the columns it lists."""
    if "Tables and columns:\n" not in prompt:
        return "[]"
    block = prompt.split("Tables and columns:\n", 1)[1].split("\n\n", 1)[0]
    listed = [c for line in block.splitlines()
              for c in line.strip().partition(": ")[2].split(", ") if c in COLUMNS]
    return json.dumps(_model_rows(listed))


def _poll(fn, *args) -> dict:
    end = time.monotonic() + JOB_WAIT
    while time.monotonic() < end:
        res = asyncio.run(fn(*args))
        if res.get("status") != "running":
            return res
        time.sleep(0.05)
    raise AssertionError("timed out")


def _wizard_discover(world) -> dict:
    entry = world.catalog.get(SID)
    disc = asyncio.run(onboard.onboard_discover(DiscoverRequest(
        system_id=SID, connection_string=entry["connection_string"],
        connection_type=entry["connection_type"], connection_config={})))
    assert "error" not in disc, disc
    return disc


def _wizard_provision(world, disc: dict, mappings: list) -> dict:
    entry = world.catalog.get(SID)
    started = asyncio.run(onboard.onboard_provision(ProvisionRequest(
        system_id=SID, system_name=entry["name"], plant=entry["plant"],
        schema=disc.get("schema_name") or "", rules=[], mappings=mappings,
        connection_type=entry["connection_type"], connection_string=entry["connection_string"],
        connection_config={})))
    return _poll(onboard.onboard_provision_status, started["job_id"])


def _field_names_written(world) -> set:
    return {p["name"] for c, p in world.writes if c.startswith("MERGE (f:Field {id: $fid})")}


def _field_types_written(world) -> dict:
    return {p["fid"]: p["dtype"] for c, p in world.writes if "SET f.data_type" in c}


def test_the_wizard_maps_and_provisions_a_namespace_of_dotted_tag_paths(monkeypatch):
    world = _namespace_world(monkeypatch)
    monkeypatch.setattr(onboard, "bedrock_call", _map_model)
    disc = _wizard_discover(world)
    started = asyncio.run(onboard.onboard_map(MapRequest(system_id=SID, tables=disc["tables"])))
    mapped = _poll(onboard.onboard_map_status, started["job_id"])
    assert mapped.get("status") == "done", mapped
    assert not any(m.get("reconciled") for m in mapped["mappings"])
    assert {m["field"] for m in mapped["mappings"]} == FULL
    done = _wizard_provision(world, disc, mapped["mappings"])
    assert done.get("status") == "done", done
    assert world.catalog.get(SID)["status"] == "onboarded"
    assert {m["field"] for m in world.job_bodies[0].mappings} == FULL
    assert _field_names_written(world) == set(COLUMNS)
    assert _field_types_written(world) == {f"{SID}.{TABLE}.{c}": t for c, t in COLUMNS.items()}


def test_a_map_result_in_both_forms_provisions(monkeypatch):
    """A map result that carries both forms (the map step of an earlier build
    kept each row as the model wrote it) provisions: provision reads each
    field against the columns discover recorded."""
    world = _namespace_world(monkeypatch)
    disc = _wizard_discover(world)
    revision = disc["tables"][0]["schema_revision"]
    done = _wizard_provision(world, disc, _stamped(_both_forms(), revision))
    assert done.get("status") == "done", done
    assert {m["field"] for m in world.job_bodies[0].mappings} == FULL
    assert _field_names_written(world) == set(COLUMNS)


def test_the_chat_provisions_the_same_namespace(monkeypatch):
    world = _namespace_world(monkeypatch)
    monkeypatch.setattr(oa, "bedrock_call", _chat_map_model)
    _chat_until_resolved()
    session_fields = {m["field"] for m in world.store.get_session(SID)["mappings"]}
    assert session_fields - FULL, "the chat's map must carry the unprefixed form too"
    finished = _chat_provision_answer()
    assert finished.get("status") == "onboarded", finished
    assert {m["field"] for m in world.job_bodies[0].mappings} == FULL
    assert _field_names_written(world) == set(COLUMNS)
