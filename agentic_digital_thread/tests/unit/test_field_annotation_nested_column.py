# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Field notes and concept repoints on NESTED (dotted) column names.

A flattened struct member is one column whose name carries dots
(``struct_a.inner_b.leaf`` on ``table_x``). The Field id is
``<system>.<table>.<column>`` whatever the column holds, so matching by that
id cannot pick the wrong split point. Splitting at the LAST dot probed a
``SchemaEntry`` named ``table_x.struct_a.inner_b`` that does not exist and
refused a real column.
"""
from __future__ import annotations

import pytest

import agents.self_learning as learning
from agents import schema_check

SYSTEM = "system-a"
NESTED = "table_x.struct_a.inner_b.leaf"


def _annotation(from_field: str) -> dict:
    return {"from_system": SYSTEM, "from_field": from_field,
            "query_context": "Auto-analysis: leaf carries a coded colour value"}


def _wire(monkeypatch, existing_fid: str):
    probes: list[dict] = []
    writes: list[tuple[str, dict]] = []

    def rows_matched(cypher, params):
        probes.append(params)
        return 1 if params.get("fid") == existing_fid else 0

    monkeypatch.setattr(learning, "_graph_rows_matched", rows_matched)
    monkeypatch.setattr(learning, "_neptune_write_loud",
                        lambda cypher, params=None: writes.append((cypher, params or {})))
    return probes, writes


@pytest.mark.parametrize("from_field", [NESTED, "table_x.struct_a.leaf"])
def test_field_annotation_on_a_nested_column_applies(monkeypatch, from_field):
    fid = f"{SYSTEM}.{from_field}"
    probes, writes = _wire(monkeypatch, fid)

    node = learning._apply_field_annotation(_annotation(from_field), "subject", 3)

    assert probes and all(p["fid"] == fid for p in probes)
    assert len(writes) == 1
    cypher, params = writes[0]
    assert "SET f.disambiguation = $annotation" in cypher
    assert "f:Field {id: $fid}" in cypher
    assert params["fid"] == fid and params["sid"] == SYSTEM
    assert params["annotation"] == "leaf carries a coded colour value"
    assert params["ver"] == 3
    assert node == f"field-{SYSTEM}-table_x-{from_field.split('.', 1)[1]}"


def test_field_annotation_on_a_flat_column_is_unchanged(monkeypatch):
    probes, writes = _wire(monkeypatch, f"{SYSTEM}.orders.line")
    node = learning._apply_field_annotation(_annotation("orders.line"), "subject", 1)
    assert probes[0]["fid"] == f"{SYSTEM}.orders.line"
    assert writes[0][1]["fid"] == f"{SYSTEM}.orders.line"
    assert node == f"field-{SYSTEM}-orders-line"


def test_field_annotation_on_a_missing_nested_column_fails_loud(monkeypatch):
    _probes, writes = _wire(monkeypatch, f"{SYSTEM}.table_x.other")
    with pytest.raises(ValueError, match="does not exist in the knowledge graph"):
        learning._apply_field_annotation(_annotation(NESTED), "subject", 1)
    assert writes == []


@pytest.mark.parametrize("from_field", ["", "orders", ".line", "orders."])
def test_field_annotation_without_a_table_and_column_writes_nothing(
        monkeypatch, from_field):
    probes, writes = _wire(monkeypatch, "never")
    node = learning._apply_field_annotation(_annotation(from_field), "subject", 1)
    assert node == "annotation-subject"
    assert probes == [] and writes == []


def test_repoint_to_a_nested_column_writes_that_exact_field_id(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(learning, "_repoint_require_field",
                        lambda fid, *_a: seen.setdefault("require", fid))
    monkeypatch.setattr(learning, "_repoint_next_version", lambda *_a: 2)
    monkeypatch.setattr(learning, "_repoint_replace_edges",
                        lambda fid, *_a: seen.setdefault("write", fid))
    learning._repoint_maps_to({"to_system": SYSTEM, "concept": "concept-a",
                               "to_field": NESTED, "confidence": 1.0})
    assert seen == {"require": f"{SYSTEM}.{NESTED}", "write": f"{SYSTEM}.{NESTED}"}


def test_schema_check_reads_the_nested_column_from_the_first_dot():
    schema = {"table_x": {"columns": ["struct_a.inner_b.leaf"]}}
    assert schema_check.column_in_schema(schema, NESTED) is True
