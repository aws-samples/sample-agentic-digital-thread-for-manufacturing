# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Equipment ValueMapping generation isolates failures per registry and per bridge.

Workshop test event 29: the first maintenance system onboarded produced its one
mapping (a unique identifier hit — deterministic, no model call), the second
(a multi-site registry whose rows are type words the deterministic pass cannot
all settle) produced ZERO although the hierarchy held matching assets. In the
generator as it stood, ONE exception anywhere in a run — one registry's row
read, or the LLM vocabulary bridge for the leftovers (a model-access or
throttling failure after retries) — propagated to the outer ``except`` and
returned ``[]``: every deterministic match of every registry in that run was
discarded. Exactly the registry that NEEDS the bridge lost its deterministic
matches too. These tests pin the isolation: the bridge failing keeps the
deterministic matches, and one registry failing keeps the other's matches.
The live cause of the event-29 zero is NOT established by these tests (the
provision log lines ``Equipment ValueMapping: ...`` say which precondition
failed there); they fix the one defect found on that path.
"""
from __future__ import annotations

import agents.onboard as onboard


def _install_graph(monkeypatch, *, registry_rows, assets):
    def fake_query(cypher, params=None):
        params = params or {}
        if "Concept {id: $concept}" in cypher:
            return list(registry_rows)
        if "RETURN f.name AS fname" in cypher:
            return [{"fname": f} for f in
                    ("asset_tag", "asset_name", "asset_type", "facility", "area_code")]
        if "(:Asset)-[:FROM_SYSTEM]" in cypher:
            return [{"sys_id": "hist-a"}]
        if "HAS_PROPERTY" in cypher:
            rows = [dict(a) for a in assets]
            if "CONTAINS $site" in cypher:
                rows = [a for a in rows if params["site"] in a["path"].lower()]
                if "line" in params:
                    rows = [a for a in rows if params["line"] in a["path"]]
            return rows
        return []
    monkeypatch.setattr(onboard, "neptune_query", fake_query)


_ASSETS = [
    {"name": "Line1 Coolant Pump", "path": "/northside/Line1/Coolant Pump",
     "source_id": "el-pump", "system": "hist-a"},
    {"name": "Line1 Press", "path": "/northside/Line1/Press",
     "source_id": "el-press", "system": "hist-a"},
    {"name": "Line1 Oven", "path": "/northside/Line1/Oven",
     "source_id": "el-oven", "system": "hist-a"},
]
# One row the deterministic type match settles, one it cannot (no type word
# in any asset name) — the second is a leftover for the LLM bridge.
_ROWS = [("PUMP-001", "Main Coolant Pump", "Pump", "Northside", "NS-L1"),
         ("HEAT-001", "Heat Treatment Furnace", "furnace", "Northside", "NS-L1")]


def test_bridge_failure_keeps_the_deterministic_matches(monkeypatch):
    _install_graph(
        monkeypatch, assets=_ASSETS,
        registry_rows=[{"sys_id": "maint-a", "db_schema": "maint_a",
                        "table_name": "assets", "field_name": "asset_tag"}])
    monkeypatch.setattr(onboard, "pg", lambda schema, sql, params=None: {"rows": list(_ROWS)})

    def boom(_prompt):
        raise RuntimeError("bedrock_call exhausted all retries")
    monkeypatch.setattr(onboard, "bedrock_call", boom)

    out = onboard._generate_equipment_value_mappings()
    assert [(vm["from_value"], vm["to_value"], vm["method"]) for vm in out] == [
        ("PUMP-001", "Line1 Coolant Pump", "hierarchy_position")]


def test_one_registry_failing_keeps_the_other_registry_matches(monkeypatch):
    _install_graph(
        monkeypatch, assets=_ASSETS,
        registry_rows=[
            {"sys_id": "maint-a", "db_schema": "maint_a",
             "table_name": "assets", "field_name": "asset_tag"},
            {"sys_id": "maint-b", "db_schema": "maint_b",
             "table_name": "assets", "field_name": "asset_tag"},
        ])

    def fake_pg(schema, sql, params=None):
        if schema == "maint_a":
            raise RuntimeError("relation does not exist")
        return {"rows": [_ROWS[0]]}
    monkeypatch.setattr(onboard, "pg", fake_pg)
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda _p: (_ for _ in ()).throw(AssertionError("no leftovers")))

    out = onboard._generate_equipment_value_mappings()
    assert [(vm["from_system"], vm["from_value"], vm["to_value"]) for vm in out] == [
        ("maint-b", "PUMP-001", "Line1 Coolant Pump")]


def test_bridge_failure_is_logged_without_exception_text(monkeypatch, caplog):
    _install_graph(
        monkeypatch, assets=_ASSETS,
        registry_rows=[{"sys_id": "maint-a", "db_schema": "maint_a",
                        "table_name": "assets", "field_name": "asset_tag"}])
    monkeypatch.setattr(onboard, "pg", lambda schema, sql, params=None: {"rows": list(_ROWS)})

    def boom(_prompt):
        raise RuntimeError("AccessDeniedException: secret-looking detail")
    monkeypatch.setattr(onboard, "bedrock_call", boom)

    with caplog.at_level("WARNING", logger=onboard.logger.name):
        onboard._generate_equipment_value_mappings()
    messages = [r.getMessage() for r in caplog.records if "vocabulary bridge" in r.getMessage()]
    assert len(messages) == 1
    assert "RuntimeError" in messages[0] and "maint-a" in messages[0]
    assert "secret-looking detail" not in messages[0]
