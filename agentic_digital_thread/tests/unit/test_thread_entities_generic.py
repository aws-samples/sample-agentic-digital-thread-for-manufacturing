# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``thread_entities`` is generic: graph-mapped identifier fields on onboarded
catalog systems, else a STRUCTURED EMPTY result (ADR-0065, 2026-09-18).

Follow-up to the neutrality work: the entity picker still carried a
hard-coded demo-schema fallback (fixed schema names, ``pg`` reads) that ran
whenever the graph had no identifier mappings — the last domain literal in
``agents/resolve.py`` (ratchet baseline 1). A fresh engine with no onboarded
systems must answer with empty lists, never with reads against schemas that
only the demo seed creates. Placeholder ids only (ADR-0044).
"""
from __future__ import annotations

import agents.resolve as resolve


def _no_pg(*_a, **_k):
    raise AssertionError("no direct schema read may run without a graph mapping")


def test_empty_graph_returns_structured_empty_result_without_schema_reads(monkeypatch):
    monkeypatch.setattr(resolve, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(resolve, "pg", _no_pg)
    monkeypatch.setattr(resolve, "query_system", _no_pg)
    out = resolve.thread_entities()
    assert set(out) == {"batch", "equipment", "product", "order"}
    assert all(v == [] for v in out.values())


def test_graph_mapped_identifier_fields_drive_the_picker(monkeypatch):
    def fake_graph(_cypher, params):
        if params["cid"] == "batch":
            return [{"sys_id": "sysA", "field": "lot_code", "schema_entry": "sysA.runs"}]
        return []
    reads: list[tuple] = []

    def fake_pg(schema, sql):
        reads.append((schema, sql))
        return {"columns": ["val"], "rows": [["L-1"], ["L-2"]]}
    monkeypatch.setattr(resolve, "neptune_query", fake_graph)
    monkeypatch.setattr(resolve, "pg", fake_pg)
    monkeypatch.setattr(resolve, "query_system", _no_pg)
    monkeypatch.setattr(resolve.catalog, "get", lambda sid: {
        "status": "onboarded", "connection_type": "PostgreSQL", "schema": "schema_a",
    } if sid == "sysA" else None)
    out = resolve.thread_entities()
    assert out["batch"] == [{"id": "L-1", "label": "L-1"}, {"id": "L-2", "label": "L-2"}]
    assert out["equipment"] == [] and out["product"] == [] and out["order"] == []
    assert reads and reads[0][0] == "schema_a" and "lot_code" in reads[0][1]


def test_resolve_module_carries_no_fixed_schema_names():
    import inspect
    source = inspect.getsource(resolve.thread_entities)
    for needle in ("mes_", "cmms_", "plm_", "erp_"):
        assert needle not in source, f"fixed schema literal {needle!r} in thread_entities"


def test_thread_entities_is_within_the_function_length_gate():
    import ast
    import inspect
    node = ast.parse(inspect.getsource(resolve.thread_entities)).body[0]
    assert node.end_lineno - node.lineno + 1 <= 50
