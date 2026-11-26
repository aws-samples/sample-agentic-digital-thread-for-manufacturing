# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The MES equipment registry against real PostgreSQL (PG lane).

Builds the two databases a workshop account has (``story_pg_support``: ERP
from ``lambda-db-init``, the seed database from ``lambda-adt-seed`` 001 / 003
/ 005 / 011, plus 009 here for the frozen historian registry) and writes the
live-story history. Then reads ONLY the onboarded schemas the workshop
manifest names (``agents.demo.workshop``), never ``public``:

* the story machine's name is one string in the live MES equipment view, the
  frozen MES equipment table, the site CMMS ``assets.description`` and the
  frozen historian's ``equipment_registry.equipment_name``;
* every line a live MES work order ran on has exactly one equipment row;
* every machine a frozen MES run names is in the frozen equipment registry.

Story values come from the story constants (``erp_simulation.condition_monitor``)
and schema names from the manifest, never literals. Skipped unless
``ERP_SIM_TEST_PG_DSN`` names a throwaway server. Run (from
``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_equipment_registry_pg.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents.demo import workshop  # noqa: E402
from tests.integration import story_pg_support as support  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
#: The seed file of the frozen historian (its equipment registry).
_HISTORIAN_SEED_NUMBER = "009"


def _manifest(system_id: str, entries) -> dict:
    (entry,) = [e for e in entries if e["id"] == system_id]
    return entry


#: The onboarded systems, as the workshop manifest registers them.
LIVE_MES = workshop.LIVE_MES_SYSTEM
FROZEN_MES = _manifest(LIVE_MES["id"], workshop.WORKSHOP_SEEDED_SYSTEMS)
HISTORIAN = [e for e in workshop.WORKSHOP_SEEDED_SYSTEMS
             if e["system_type"] == "Historian" and e["schema"]][0]
CMMS = [e for e in workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS if e["system_type"] == "CMMS"][0]


@pytest.fixture(scope="module")
def story_db():
    """{erp|seed: connection} after db-init, the seed files (with the
    historian) and the live story history; ``routed_pg`` over them."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "eqm") as (conns, _plan):
        (path,) = (_REPO / "lambda-adt-seed").glob(f"{_HISTORIAN_SEED_NUMBER}_*.sql")
        with conns["seed"].cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
        conns["seed"].commit()
        yield conns, support.routed_pg(conns)


def _rows(pg, schema: str, sql, params=None) -> list:
    return [tuple(r) for r in pg(schema, sql, params)["rows"]]


def test_the_story_machine_has_one_name_in_the_mes_and_the_cmms(story_db):
    from erp_simulation import condition_monitor as cm

    _conns, pg = story_db
    live = _rows(pg, LIVE_MES["schema"], "SELECT line, equipment_name FROM equipment "
                                         "WHERE equipment_id = %s", (cm.STORY_MES_EQUIPMENT_ID,))
    frozen = _rows(pg, FROZEN_MES["schema"], "SELECT equipment_name FROM equipment "
                                             "WHERE equipment_id = %s", (cm.STORY_MES_EQUIPMENT_ID,))
    cmms = _rows(pg, CMMS["schema"], "SELECT description FROM assets WHERE asset_num = %s",
                 (cm.STORY_CMMS_ASSET_NUM,))
    historian = _rows(pg, HISTORIAN["schema"], "SELECT equipment_name FROM equipment_registry "
                                               "WHERE equipment_id = %s", (cm.STORY_SITEWISE_ASSET,))
    assert len(live) == len(frozen) == len(cmms) == len(historian) == 1
    assert live[0][0] == cm.STORY_MES_LINE
    assert live[0][1] == frozen[0][0] == cmms[0][0] == historian[0][0]


def test_every_line_a_live_work_order_ran_on_has_one_machine(story_db):
    _conns, pg = story_db
    schema = LIVE_MES["schema"]
    lines = {line for (line,) in _rows(pg, schema, "SELECT DISTINCT line FROM work_orders")}
    assert lines, "the live story history wrote no work orders"
    counts = dict(_rows(pg, schema, "SELECT line, count(*) FROM equipment GROUP BY line"))
    assert {line: counts.get(line, 0) for line in lines} == {line: 1 for line in lines}


def test_every_frozen_run_names_a_machine_of_the_equipment_registry(story_db):
    _conns, pg = story_db
    schema = FROZEN_MES["schema"]
    used = {eid for (eid,) in _rows(pg, schema, "SELECT DISTINCT equipment_id FROM production_runs")}
    registry = {eid for (eid,) in _rows(pg, schema, "SELECT equipment_id FROM equipment")}
    assert used and used == registry


# --------------------------------------------------------------------------
# The cross-registry name match (agents.onboard, ADR-0098) on the real rows.
# The graph is stubbed only where it names a table's Fields: from the real
# columns. Each registry is (system, schema, table, tag column), the tag
# column being the one module 4 and the seed map to 'equipment-id'.
# --------------------------------------------------------------------------
def _registry(entry: dict, table: str, tag_field: str) -> dict:
    return {"sys_id": entry["id"], "db_schema": entry["schema"], "table": table,
            "tag_field": tag_field, "plant": entry.get("plant", "")}


def _name_match(monkeypatch, pg, registries: list) -> list:
    from agents import onboard

    schema_of = {r["sys_id"]: r["db_schema"] for r in registries}

    def field_list(cypher, params=None):
        assert "RETURN f.name AS fname" in cypher, cypher
        cols = _rows(pg, schema_of[params["sid"]],
                     "SELECT column_name FROM information_schema.columns "
                     "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                     (schema_of[params["sid"]], params["tbl"]))
        return [{"fname": c} for (c,) in cols]

    monkeypatch.setattr(onboard, "neptune_query", field_list)
    monkeypatch.setattr(onboard, "pg", pg)
    named = [onboard._registry_name_rows(r) for r in registries]
    assert all(named), "a registry has no name column"
    return sorted((vm["from_system"], vm["from_value"], vm["to_system"], vm["to_value"],
                   vm["method"], vm["confidence"]) for vm in onboard._cross_registry_name_vms(named))


def test_live_mes_and_site_cmms_pair_exactly_the_story_machine(story_db, monkeypatch):
    from erp_simulation import condition_monitor as cm

    _conns, pg = story_db
    pairs = _name_match(monkeypatch, pg, [_registry(LIVE_MES, "equipment", "equipment_id"),
                                          _registry(CMMS, "assets", "asset_num")])
    ends = sorted([(CMMS["id"], cm.STORY_CMMS_ASSET_NUM), (LIVE_MES["id"], cm.STORY_MES_EQUIPMENT_ID)])
    assert pairs == [(*ends[0], *ends[1], "equipment_name_match", 0.95)]


def test_frozen_mes_cmms_and_historian_pair_only_the_story_machine(story_db, monkeypatch):
    from erp_simulation import condition_monitor as cm

    _conns, pg = story_db
    pairs = _name_match(monkeypatch, pg, [_registry(FROZEN_MES, "equipment", "equipment_id"),
                                          _registry(CMMS, "assets", "asset_num"),
                                          _registry(HISTORIAN, "equipment_registry", "equipment_id")])
    ends = {CMMS["id"]: cm.STORY_CMMS_ASSET_NUM, FROZEN_MES["id"]: cm.STORY_MES_EQUIPMENT_ID,
            HISTORIAN["id"]: cm.STORY_SITEWISE_ASSET}
    ids = sorted(ends)
    expected = sorted((a, ends[a], b, ends[b], "equipment_name_match", 0.95)
                      for i, a in enumerate(ids) for b in ids[i + 1:])
    assert pairs == expected and len(pairs) == 3


# --------------------------------------------------------------------------
# The module 4 review trap gates the name match, on the real rows: the whole
# generator (registry discovery, the tag pick with its value sample, the row
# reads) runs over the seeded tables; the graph is stubbed with the Fields on
# 'equipment-id' and the tables' real columns, and no historian, so only the
# name match can write.
# --------------------------------------------------------------------------
#: Every site CMMS ``assets`` column the seed's mapper put on 'equipment-id'
#: on two fresh accounts (2026-10-03, the measured shape).
_MEASURED_CMMS_EQUIPMENT_ID_FIELDS = ("asset_num", "description", "install_date",
                                      "manufacturer", "model_number", "serial_number")


def _trapped_fields(system_id: str) -> set:
    """The ``assets`` fields the workshop override moves off 'equipment-id'."""
    import json
    rows = json.loads(Path(workshop.mapping_overrides_path()).read_text())[system_id]
    return {r["field"].split(".", 1)[1] for r in rows if r.get("table") == "assets"
            and "equipment-id" not in {m["concept"] for m in r["mappings"]}}


def _generate(monkeypatch, pg, cmms_fields) -> list:
    on_concept = [(LIVE_MES, "equipment", "equipment_id")] + [
        (CMMS, "assets", f) for f in cmms_fields]
    return _generate_over(monkeypatch, pg, on_concept)


def _generate_over(monkeypatch, pg, on_concept) -> list:
    """The whole generator over the real rows; the graph names each
    ``(entry, table, field)`` of ``on_concept`` on 'equipment-id' (no
    identity key, in that order) and each table's real columns, and no
    historian Asset. Returns (from, value, to, value, method) tuples."""
    from agents import onboard

    _install_generation(monkeypatch, pg, on_concept)
    return sorted((vm["from_system"], vm["from_value"], vm["to_system"], vm["to_value"],
                   vm["method"]) for vm in onboard._generate_equipment_value_mappings())


def _install_generation(monkeypatch, pg, on_concept) -> None:
    """The graph reads of ``_generate_over``, the real ``pg`` and no model."""
    from agents import onboard

    schema_of = {e["id"]: e["schema"] for e, _t, _f in on_concept}

    def graph(cypher, params=None):
        if "Concept {id: $concept}" in cypher:
            return [{"sys_id": e["id"], "db_schema": e["schema"], "plant": e["plant"],
                     "table_name": t, "field_name": f, "is_key": False} for e, t, f in on_concept]
        if "RETURN f.name AS fname" in cypher:
            schema = schema_of[params["sid"]]
            return [{"fname": c} for (c,) in _rows(
                pg, schema, "SELECT column_name FROM information_schema.columns WHERE "
                "table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, params["tbl"]))]
        return []

    def no_model(_prompt):
        raise AssertionError("the equipment pass called the model")

    monkeypatch.setattr(onboard, "neptune_query", graph)
    monkeypatch.setattr(onboard, "pg", pg)
    monkeypatch.setattr(onboard, "bedrock_call", no_model)


def test_the_review_trap_gates_the_mes_cmms_name_match_on_the_real_rows(story_db, monkeypatch):
    from erp_simulation import condition_monitor as cm

    _conns, pg = story_db
    trapped = _trapped_fields(CMMS["id"])
    assert trapped == {"asset_num"}
    untouched = [f for f in _MEASURED_CMMS_EQUIPMENT_ID_FIELDS if f not in trapped]
    assert _generate(monkeypatch, pg, untouched) == []

    corrected = _generate(monkeypatch, pg, _MEASURED_CMMS_EQUIPMENT_ID_FIELDS)
    ends = sorted([(CMMS["id"], cm.STORY_CMMS_ASSET_NUM), (LIVE_MES["id"], cm.STORY_MES_EQUIPMENT_ID)])
    assert corrected == [(*ends[0], *ends[1], "equipment_name_match")]


# --------------------------------------------------------------------------
# The frozen story through registry discovery, on the real rows. Each
# system's tag column is also a column of the fact tables that reference it
# (MES runs, historian readings and metrics, CMMS PM schedules and work
# orders), and the mapper can put any of them on 'equipment-id': the graph
# here carries all of them, with no identity key, in either order, plus the
# CMMS columns measured on 'equipment-id' (2026-10-03) and one MES column
# that names equipment other than the machines (the gauges of a quality
# check), designation-dense where the MES tags are not. The pick must take
# each system's equipment reference table, and the generator must write exactly the
# three name matches.
# --------------------------------------------------------------------------
#: Per system, every (table, column) that holds its equipment tag, the
#: equipment reference table's last.
_FROZEN_TAG_COLUMNS = {
    FROZEN_MES["id"]: (FROZEN_MES, [("production_runs", "equipment_id"),
                                    ("equipment", "equipment_id")]),
    HISTORIAN["id"]: (HISTORIAN, [("sensor_readings", "equipment_id"),
                                  ("aggregated_metrics", "equipment_id"),
                                  ("equipment_registry", "equipment_id")]),
    CMMS["id"]: (CMMS, [("work_orders", "asset_num"), ("pm_schedules", "asset_num"),
                        ("assets", "asset_num")]),
}
_FROZEN_OTHER_EQUIPMENT_ID_COLUMNS = [(FROZEN_MES, "quality_checks", "measurement_equipment")] + [
    (CMMS, "assets", f) for f in _MEASURED_CMMS_EQUIPMENT_ID_FIELDS if f != "asset_num"]


def _frozen_graph(order: str) -> list:
    """(entry, table, field) on 'equipment-id': fact tables first, or the
    reference tables first."""
    tags = [(e, t, f) for e, cols in _FROZEN_TAG_COLUMNS.values() for t, f in cols]
    rows = tags + _FROZEN_OTHER_EQUIPMENT_ID_COLUMNS
    return rows if order == "facts first" else rows[::-1]


@pytest.mark.parametrize("order", ["facts first", "reference tables first"])
def test_the_frozen_story_pairs_its_three_registries_through_discovery(story_db, monkeypatch,
                                                                        order):
    from agents import onboard
    from erp_simulation import condition_monitor as cm

    _conns, pg = story_db
    pairs = _generate_over(monkeypatch, pg, _frozen_graph(order))
    picked = sorted((r["sys_id"], r["table"], r["tag_field"])
                    for r in onboard._discover_equipment_registries())
    assert picked == sorted((sid, *cols[-1]) for sid, (_e, cols) in _FROZEN_TAG_COLUMNS.items())

    ends = {CMMS["id"]: cm.STORY_CMMS_ASSET_NUM, FROZEN_MES["id"]: cm.STORY_MES_EQUIPMENT_ID,
            HISTORIAN["id"]: cm.STORY_SITEWISE_ASSET}
    ids = sorted(ends)
    expected = sorted((a, ends[a], b, ends[b], "equipment_name_match")
                      for i, a in enumerate(ids) for b in ids[i + 1:])
    assert pairs == expected and len(pairs) == 3


# --------------------------------------------------------------------------
# The reconcile on the workshop path: the frozen
# story's three name matches, from discovery over the real rows, written by
# the post-pass persist into an in-memory ValueMapping graph
# (``tests.unit._equipment_vm_graph``). A name match a previous provision
# wrote between two of the registries, for a machine that has since been
# renamed, goes on the first pass; a steward row between the same two stays.
# A second, identical pass deletes nothing and leaves exactly the same rows.
# --------------------------------------------------------------------------
def test_a_second_identical_pass_on_the_workshop_rows_sweeps_nothing(story_db, monkeypatch):
    from agents import onboard
    from erp_simulation import condition_monitor as cm
    from tests.unit._equipment_vm_graph import EquipmentVmGraph

    _conns, pg = story_db
    ends = {CMMS["id"]: cm.STORY_CMMS_ASSET_NUM, FROZEN_MES["id"]: cm.STORY_MES_EQUIPMENT_ID,
            HISTORIAN["id"]: cm.STORY_SITEWISE_ASSET}
    ids = sorted(ends)
    graph = EquipmentVmGraph(ids).install(monkeypatch)
    renamed = {"from_system": ids[0], "from_value": "RENAMED-A", "to_system": ids[1],
               "to_value": "RENAMED-B", "concept": "equipment", "method": "equipment_name_match"}
    graph.add(onboard._equipment_vm_id(renamed), **renamed)
    graph.add("subj-0000000000000001", **dict(renamed, method="user_confirmed"))
    _install_generation(monkeypatch, pg, _frozen_graph("facts first"))

    assert onboard._persist_equipment_value_mappings() == 3
    expected = sorted((a, ends[a], b, ends[b], "equipment_name_match")
                      for i, a in enumerate(ids) for b in ids[i + 1:])
    assert graph.rows() == expected
    assert "subj-0000000000000001" in graph.nodes
    assert len(graph.statements("_RETIRE_STALE_EQUIPMENT_VMS")) == 1
    nodes = {vm_id: dict(props) for vm_id, props in graph.nodes.items()}

    assert onboard._persist_equipment_value_mappings() == 3
    assert len(graph.statements("_RETIRE_STALE_EQUIPMENT_VMS")) == 1
    assert graph.nodes == nodes
