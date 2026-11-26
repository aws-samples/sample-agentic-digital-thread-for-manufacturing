# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0077 D1/D5 — the ONE SchemaEntry/Field writer home (offline, pure).

Pins: the Cypher text is byte-identical to the pre-ADR-0077 b2a writer (the
literals live HERE, not imported); the exact write params; identifiers fail
closed; the old literal is grep-gated out of every other ``agents/`` module;
every ``f.<prop>`` a reader projects is written by ``FIELD_MERGE`` (a
materialised Field is immediately scorable); the module is import-pure.
"""
from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed internal argv list only
import sys
from pathlib import Path

import pytest

import agents.schema_fields as sf

_ADT = Path(__file__).resolve().parents[2]
_AGENTS = _ADT / "agents"

_PRE_ADR_SCHEMA_ENTRY = (
    "MERGE (se:SchemaEntry {id: $seid}) SET se.name = $name, se.system_id = $sid "
    "WITH se MERGE (s:System {id: $sid}) ON CREATE SET s.name = $sid, s.status = 'onboarded' "
    "MERGE (s)-[:HAS_SCHEMA]->(se)")
_PRE_ADR_FIELD = (
    "MERGE (f:Field {id: $fid}) SET f.name = $name, f.schema_id = $schema_id, "
    "f.system_id = $sid, f.is_partition = $isp, f.data_type = $dtype")
_PRE_ADR_HAS_FIELD = (
    "MATCH (se:SchemaEntry {id: $seid}), (f:Field {id: $fid}) MERGE (se)-[:HAS_FIELD]->(f)")


def _spy():
    calls: list = []
    return calls, (lambda cypher, params=None: calls.append((cypher, params)) or True)


def test_cypher_is_byte_identical_to_the_pre_adr_writer():
    assert sf.SCHEMA_ENTRY_MERGE == _PRE_ADR_SCHEMA_ENTRY
    assert sf.FIELD_MERGE == _PRE_ADR_FIELD
    assert sf.HAS_FIELD_MERGE == _PRE_ADR_HAS_FIELD


def test_write_physical_field_exact_params():
    calls, write = _spy()
    fid = sf.write_physical_field(
        "erp", "work_orders",
        {"name": "priority", "data_type": "text", "is_partition": False}, write=write)
    assert fid == "erp.work_orders.priority"
    assert calls == [
        (sf.FIELD_MERGE, {"fid": "erp.work_orders.priority", "name": "priority",
                          "schema_id": "erp.work_orders", "sid": "erp",
                          "isp": False, "dtype": "text"}),
        (sf.HAS_FIELD_MERGE, {"seid": "erp.work_orders",
                              "fid": "erp.work_orders.priority"}),
    ]
    assert list(calls[0][1]) == ["fid", "name", "schema_id", "sid", "isp", "dtype"]


def test_write_schema_entry_and_completeness_params():
    calls, write = _spy()
    assert sf.write_schema_entry("erp", "segment_events", write=write) == "erp.segment_events"
    sf.write_discovery_completeness("erp", "segment_events", False, 2, write=write)
    assert calls == [
        (sf.SCHEMA_ENTRY_MERGE, {"seid": "erp.segment_events",
                                 "name": "segment_events", "sid": "erp"}),
        (sf.DISCOVERY_COMPLETENESS_SET, {"seid": "erp.segment_events",
                                         "complete": False, "count": 2}),
    ]


@pytest.mark.parametrize("bad", ["", "a b", "a;b", "a.b", "x" * 129, None, 7])
def test_validate_table_name_fails_closed(bad):
    with pytest.raises(ValueError):
        sf.validate_table_name(bad)


@pytest.mark.parametrize("good", ["work_orders", "t-1", "x" * 128])
def test_validate_table_name_accepts_tokens(good):
    assert sf.validate_table_name(good) == good


@pytest.mark.parametrize("bad", [{"name": "1x"}, {"name": "a..b"}, {"name": "a-b"},
                                 {"name": ""}, {}, "priority", None])
def test_validate_physical_column_fails_closed(bad):
    with pytest.raises(ValueError):
        sf.validate_physical_column(bad)


def test_validate_physical_column_returns_a_new_normalised_dict():
    src = {"name": "oee.availability", "data_type": "double", "is_partition": 0, "x": 1}
    out = sf.validate_physical_column(src)
    assert out == {"name": "oee.availability", "data_type": "double", "is_partition": False}
    assert out is not src and "x" in src


def test_readers_and_probes():
    def query(cypher, params=None):
        if cypher == sf.FIELD_READBACK:
            return [{"name": "priority"}] if params["fid"] == "erp.work_orders.priority" else []
        if cypher == sf.SCHEMA_ENTRY_EXISTS:
            return [{"n": 1 if params["seid"] == "erp.work_orders" else 0}]
        if cypher == sf.TABLE_FIELD_NAMES:
            return [{"name": "id"}, {"name": "priority"}, {"name": None}]
        return []
    assert sf.read_field("erp", "work_orders", "priority", query=query) == {"name": "priority"}
    assert sf.read_field("erp", "work_orders", "other", query=query) is None
    assert sf.schema_entry_exists("erp", "work_orders", query=query) is True
    assert sf.schema_entry_exists("erp", "missing", query=query) is False
    assert sf.schema_entry_exists("erp", "x", query=lambda *_a: []) is False
    assert sf.table_field_names("erp", "work_orders", query=query) == {"id", "priority"}


def test_old_literal_lives_only_in_schema_fields():
    """Grep-gate: the Field MERGE text has ONE home under agents/."""
    offenders = [str(p.relative_to(_AGENTS)) for p in sorted(_AGENTS.rglob("*.py"))
                 if "f.is_partition = $isp" in p.read_text(encoding="utf-8")]
    assert offenders == ["schema_fields.py"], offenders


def _projected_field_props(cypher: str) -> set:
    return set(re.findall(r"\bf\.([a-z_]+)\s+AS\b", cypher))


def test_readers_project_only_props_field_merge_writes():
    """A materialised Field carries every prop the readers project (id is the
    MERGE key) — the 'immediately scorable' guarantee."""
    written = set(re.findall(r"\bf\.([a-z_]+)\s*=", sf.FIELD_MERGE)) | {"id"}
    assert written == {"id", "name", "schema_id", "system_id", "is_partition", "data_type"}
    import agents.vocabulary_drift as vd
    src = (_AGENTS / "graph_tools.py").read_text(encoding="utf-8")
    schema_query = src.split("def _schema_query", 1)[1].split("def ", 1)[0]
    reader_props = (_projected_field_props(vd._UNBOUND_FIELDS_CYPHER)
                    | _projected_field_props(schema_query)) - {"disambiguation"}
    assert reader_props, "parity scan found no projected Field props"
    assert reader_props <= written, reader_props - written


def test_module_is_import_pure():
    code = ("import sys, agents.schema_fields\n"
            "bad = [m for m in ('agents.db', 'boto3', 'agents.onboard') if m in sys.modules]\n"
            "print(','.join(bad))\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=str(_ADT),  # nosec B603 - fixed argv
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_expected_field_is_the_merged_shape():
    col = {"name": "priority", "data_type": "text", "is_partition": 0}
    assert sf.expected_field("erp", "work_orders", col) == {
        "name": "priority", "schema_id": "erp.work_orders", "system_id": "erp",
        "data_type": "text", "is_partition": False}


def test_field_mismatches_compares_every_property():
    want = sf.expected_field("erp", "work_orders", {"name": "c", "data_type": "integer"})
    assert sf.field_mismatches(want, dict(want)) == []
    assert sf.field_mismatches(want, None) == sorted(want)
    assert sf.field_mismatches(want, {}) == sorted(want)
    wrong = {"name": "wrong", "schema_id": "erp.work_orders", "system_id": "erp",
             "data_type": "varchar", "is_partition": True}
    assert sf.field_mismatches(want, wrong) == ["data_type", "is_partition", "name"]
    # A non-boolean partition flag never passes as False.
    assert sf.field_mismatches(want, {**want, "is_partition": 0}) == ["is_partition"]


def test_readbacks_project_the_full_shape():
    for prop in ("f.name", "f.schema_id", "f.system_id", "f.data_type", "f.is_partition"):
        assert prop in sf.FIELD_READBACK
    assert "(s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid})" \
        in sf.SCHEMA_ENTRY_READBACK
    seen = []
    row = sf.read_schema_entry("erp", "t", query=lambda c, p: seen.append((c, p)) or [{"name": "t"}])
    assert row == {"name": "t"}
    assert seen == [(sf.SCHEMA_ENTRY_READBACK, {"sid": "erp", "seid": "erp.t"})]
    assert sf.read_schema_entry("erp", "t", query=lambda c, p: []) is None
