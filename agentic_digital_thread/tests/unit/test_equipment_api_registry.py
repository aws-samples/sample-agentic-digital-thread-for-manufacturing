# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An equipment registry read through its connector's record listing.

A REST-family connector whose adapter declares ``listable_entities`` and a
``list_records`` hook can be an equipment registry like a SQL system: the tag
pick's evidence, the telemetry match and the cross-registry name match read
its records through the hook, under the same method, confidence, site and
evidence rules, bounded (page size, row cap) and failing soft. Before, the
pass read SQL systems only, so a registry onboarded over REST got no
mapping, not even for a machine whose name another registry carries.

Offline: a fake graph, a fake ``pg`` and a fake connector registered in the
REST vendor registry, with names unlike any shipped source. Synthetic values
only.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap

import pytest

import agents.equipment_api_registry as api_registry
import agents.onboard as onboard
import agents.rest_vendor_registry as registry
from tests.unit._equipment_vm_graph import EquipmentVmGraph

_PLANT = "Plant North"
_NAME = "Fixture Lathe QX-210"
#: A REST-family connection type outside SQL; the fake connector claims it.
_REST = "REST API"
_ENTITY = "Machinery"

#: The REST registry's records: a designated tag, a numeric key, a name.
_LEDGER = [
    {"unit_ref": "N7-QX-210", "unit_key": 501, "unit_description": _NAME,
     "plant_code": "", "bay": "B2"},
    {"unit_ref": "N7-ZZ-990", "unit_key": 502, "unit_description": "Saw ZZ-990",
     "plant_code": "", "bay": "B9"},
]
#: A SQL registry shaped like a cell list: tags without a designation.
_STATIONS = [("LN2-W1", _NAME, "LATHE", None, "LINE2"),
             ("LN2-W2", "Bench Grinder 9", "GRINDER", None, "LINE2")]
#: A SQL maintenance registry: designated tags, the same machine name.
_SHOP = [("77-QX-210", _NAME, None, None, "N-C2-L1"),
         ("77-ZZ-990", "Saw ZZ-990", None, None, "N-C2-S1")]
_HIST_ASSET = {"name": "Fixture-Lathe-QX-210", "path": "North/Cell2/Fixture-Lathe-QX-210",
               "source_id": "el-qx", "system": "hist-x"}


def _row(sys_id, table, field, *, schema=None, ctype="PostgreSQL", is_key=True, plant=_PLANT):
    return {"sys_id": sys_id, "db_schema": f"{sys_id}_db" if schema is None else schema,
            "plant": plant, "connection_type": ctype, "table_name": table,
            "field_name": field, "is_key": is_key}


class _FakeConnector:
    """A REST-family adapter that lists one entity. Duck-typed (the fields the
    registry reads), so the same fake runs against code that predates the
    listing hook."""

    name = "fixture-ledger"
    telemetry_rich = False
    structured_reader = False
    encode_registration_secret = sample_values = probe = None
    discover = provision_agent = runtime_tools = None
    listable_entities = (_ENTITY,)

    def __init__(self, records, *, partial=False, fail=False):
        self.records, self.partial, self.fail = records, partial, fail
        self.calls: list[tuple] = []
        self.deadlines: list = []
        #: Run inside each listing (a fake clock advancing, say).
        self.during = None

    def detect(self, connection_type, base_url="", entry=None):
        return connection_type == _REST

    def list_records(self, system_id, entity, *, page_size, max_rows, deadline=None):
        self.calls.append((system_id, entity, page_size, max_rows))
        self.deadlines.append(deadline)
        if self.during:
            self.during()
        if self.fail:
            raise ConnectionError("connection reset")
        return {"rows": [dict(r) if isinstance(r, dict) else r for r in self.records],
                "partial": self.partial}


class _World:
    """Graph rows routed by cypher fragment (fields per system AND table), a
    ``pg`` that answers the SQL registries' three statement shapes and raises
    for any other schema (as a schema with no tables would), and the fake
    connector."""

    def __init__(self, *, rest_schema="", rest_plant=_PLANT, shop=False, historian=False):
        self.registries = [
            _row("reg-line", "stations", "station_code"),
            _row("reg-ledger", _ENTITY, "unit_ref", schema=rest_schema, ctype=_REST,
                 is_key=False, plant=rest_plant),
            _row("reg-ledger", _ENTITY, "unit_key", schema=rest_schema, ctype=_REST,
                 plant=rest_plant),
            _row("reg-ledger", "WorkLog", "unit_ref", schema=rest_schema, ctype=_REST,
                 is_key=False, plant=rest_plant),
        ]
        self.fields = {("reg-line", "stations"): ["station_code", "station_name", "station_kind",
                                                  "line"],
                       ("reg-ledger", _ENTITY): ["unit_ref", "unit_key", "unit_description",
                                                 "plant_code", "bay"],
                       ("reg-ledger", "WorkLog"): ["unit_ref", "note"]}
        self.tables = {"reg-line_db": _STATIONS}
        if shop:
            self.registries.append(_row("reg-shop", "kit", "kit_tag"))
            self.fields[("reg-shop", "kit")] = ["kit_tag", "kit_name", "location"]
            self.tables["reg-shop_db"] = _SHOP
        self.historians = ["hist-x"] if historian else []
        self.pg_reads: list[tuple] = []
        self.unexpected: list[tuple] = []
        self.connector = _FakeConnector(_LEDGER)

    def install(self, monkeypatch) -> "_World":
        monkeypatch.setattr(onboard, "neptune_query", self.graph)
        monkeypatch.setattr(onboard, "pg", self.pg)
        monkeypatch.setattr(onboard, "bedrock_call", self.no_model)
        monkeypatch.setattr(registry, "REST_VENDOR_REGISTRY",
                            [self.connector, *registry.REST_VENDOR_REGISTRY])
        return self

    def graph(self, cypher, params=None):
        params = params or {}
        if "Concept {id: $concept}" in cypher:
            return [dict(r) for r in self.registries]
        if "RETURN f.name AS fname" in cypher:
            return [{"fname": f} for f in self.fields.get((params["sid"], params["tbl"]), [])]
        if "(:Asset)-[:FROM_SYSTEM]" in cypher:
            return [{"sys_id": h} for h in self.historians]
        if "HAS_PROPERTY" in cypher:
            if not self.historians or "CONTAINS $site" in cypher:
                return []
            return [dict(_HIST_ASSET)]
        return []

    def pg(self, schema, sql, params=None):
        self.pg_reads.append((schema, sql))
        if schema not in self.tables:
            raise RuntimeError(f'relation does not exist in schema "{schema}"')
        if re.fullmatch(r"SELECT .+ FROM \w+ ORDER BY 1", sql):
            return {"rows": [list(r) for r in self.tables[schema]]}
        self.unexpected.append((schema, sql))
        raise AssertionError(f"unexpected statement for {schema}: {sql}")

    @staticmethod
    def no_model(_prompt):
        raise AssertionError("the equipment pass called the model")


def _world(monkeypatch, **kw) -> _World:
    return _World(**kw).install(monkeypatch)


def _summary(vms) -> list[tuple]:
    return sorted((vm["from_system"], vm["from_field"], vm["from_value"], vm["to_system"],
                   vm["to_field"], vm["to_value"], vm["method"], vm["confidence"]) for vm in vms)


_LEDGER_TO_LINE = ("reg-ledger", "Machinery.unit_ref", "N7-QX-210", "reg-line",
                   "stations.station_code", "LN2-W1", "equipment_name_match", 0.95)


# --------------------------------------------------------------------------- #
# the reproduction: a REST registry with a matching name
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rest_schema", ["", "ledger"])
def test_a_rest_registry_with_a_matching_machine_name_gets_the_name_mapping(monkeypatch,
                                                                            rest_schema):
    """A REST System node carries no SQL schema (``""``); with one it was left
    out as not readable with SQL. Either way it got no mapping. Now it pairs
    with the SQL registry by machine name, with the SQL path's method,
    confidence and computed provenance."""
    world = _world(monkeypatch, rest_schema=rest_schema)
    out = onboard._generate_equipment_value_mappings()
    assert _summary(out) == [_LEDGER_TO_LINE]
    (vm,) = out
    assert "reg-ledger Machinery.unit_description" in vm["provenance"]
    assert "reg-line stations.station_name" in vm["provenance"]
    assert [s for s, _ in world.pg_reads if s != "reg-line_db"] == [] and world.unexpected == []


def test_the_rest_registry_is_listed_once_per_pass_within_the_bounds(monkeypatch):
    """The pick's evidence, the row read and the name read share one listing
    of the one entity the connector lists; the entity it does not list is
    never asked for."""
    world = _world(monkeypatch)
    onboard._generate_equipment_value_mappings()
    assert world.connector.calls == [("reg-ledger", _ENTITY, api_registry.PAGE_SIZE,
                                      api_registry.MAX_ROWS)]
    assert (api_registry.PAGE_SIZE, api_registry.MAX_ROWS) == (100, 1000)


def test_the_rest_tag_is_picked_on_its_values_not_on_its_key(monkeypatch):
    """``unit_key`` is the identity key, but its values carry no designation;
    ``unit_ref``'s do, as a SQL registry's tag column is picked. A field on an
    entity the connector does not list is no candidate."""
    _world(monkeypatch)
    regs = {r["sys_id"]: r for r in onboard._discover_equipment_registries()}
    assert (regs["reg-ledger"]["table"], regs["reg-ledger"]["tag_field"]) == (_ENTITY, "unit_ref")
    assert regs["reg-ledger"]["connection_type"] == _REST


# --------------------------------------------------------------------------- #
# same rules as SQL
# --------------------------------------------------------------------------- #

def test_a_rest_registry_gets_telemetry_mappings_by_the_same_evidence(monkeypatch):
    _world(monkeypatch, historian=True)
    out = onboard._generate_equipment_value_mappings()
    tele = [vm for vm in out if vm["to_system"] == "hist-x"]
    assert [(vm["from_system"], vm["from_field"], vm["from_value"], vm["to_value"],
             vm["to_element_id"], vm["method"], vm["confidence"]) for vm in tele] == [
        ("reg-ledger", "Machinery.unit_ref", "N7-QX-210", "Fixture-Lathe-QX-210", "el-qx",
         "value_format_match", 0.95)]


@pytest.mark.parametrize("plant_code, matches", [("77", False), (_PLANT, True), ("", False)])
def test_a_rest_registry_with_no_plant_is_sited_by_its_site_column(monkeypatch, plant_code,
                                                                   matches):
    """The site rule is the SQL one: a registry whose System has no plant is
    at the site its row's site column names; a site code that is not the
    other registry's plant, or no value, matches nothing."""
    world = _world(monkeypatch, rest_plant="")
    world.connector.records = [dict(r, plant_code=plant_code) for r in _LEDGER]
    out = onboard._generate_equipment_value_mappings()
    assert [vm["method"] for vm in out] == (["equipment_name_match"] if matches else [])


def test_the_rest_registry_pairs_with_every_registry_like_a_sql_one(monkeypatch):
    """Both machine names are in the REST and the maintenance registry; only
    the lathe is in the cell registry."""
    world = _world(monkeypatch, shop=True)
    pairs = sorted((vm["from_system"], vm["from_value"], vm["to_system"], vm["to_value"])
                   for vm in onboard._generate_equipment_value_mappings())
    assert pairs == [("reg-ledger", "N7-QX-210", "reg-line", "LN2-W1"),
                     ("reg-ledger", "N7-QX-210", "reg-shop", "77-QX-210"),
                     ("reg-ledger", "N7-ZZ-990", "reg-shop", "77-ZZ-990"),
                     ("reg-line", "LN2-W1", "reg-shop", "77-QX-210")]
    assert len(world.connector.calls) == 1


# --------------------------------------------------------------------------- #
# bounded and fail-soft
# --------------------------------------------------------------------------- #

def test_a_cut_listing_writes_nothing_for_that_registry_and_keeps_the_others(monkeypatch):
    world = _world(monkeypatch, shop=True)
    world.connector.partial = True
    out = onboard._generate_equipment_value_mappings()
    assert sorted((vm["from_system"], vm["to_system"]) for vm in out) == [("reg-line", "reg-shop")]
    assert "reg-ledger" not in out.complete_systems


def test_a_failing_listing_fails_soft_and_keeps_the_others(monkeypatch):
    world = _world(monkeypatch, shop=True)
    world.connector.fail = True
    out = onboard._generate_equipment_value_mappings()
    assert sorted((vm["from_system"], vm["to_system"]) for vm in out) == [("reg-line", "reg-shop")]
    assert "reg-ledger" not in out.complete_systems and world.connector.calls


def test_a_listing_longer_than_the_cap_is_cut(monkeypatch):
    world = _world(monkeypatch)
    world.connector.records = [{"unit_ref": f"N7-QX-{n:04d}", "unit_description": f"Lathe QX-{n:04d}"}
                               for n in range(api_registry.MAX_ROWS + 5)]
    with onboard.recording_incomplete_reads() as sink:
        rows = api_registry.records("reg-ledger", _REST, _ENTITY)
    assert len(rows) == api_registry.MAX_ROWS and sink == ["partial"]


@pytest.mark.parametrize("ctype", ["MCP Server", "SiteWise (Direct)", "Athena", "Tulip (Tables API)"])
def test_a_system_whose_connector_lists_nothing_stays_out_and_is_never_read(monkeypatch, ctype):
    world = _world(monkeypatch)
    for r in world.registries:
        if r["sys_id"] == "reg-ledger":
            r["connection_type"], r["db_schema"] = ctype, "ledger"
    regs, unread = onboard._discover_equipment_registries_and_unread()
    assert [r["sys_id"] for r in regs] == ["reg-line"] and unread == set()
    assert onboard._generate_equipment_value_mappings() == []
    assert world.connector.calls == [] and [s for s, _ in world.pg_reads if s == "ledger"] == []


@pytest.mark.parametrize("bad", ["not a record", None, 7, ["N7-QX-210", _NAME]])
def test_a_listing_with_an_item_that_is_not_a_record_is_incomplete(monkeypatch, bad):
    world = _world(monkeypatch)
    world.connector.records = [dict(_LEDGER[1]), bad]
    with onboard.recording_incomplete_reads() as sink:
        rows = api_registry.records("reg-ledger", _REST, _ENTITY)
    assert sink == ["malformed_listing"]
    assert rows == [dict(_LEDGER[1])]


def test_a_malformed_item_writes_and_retires_nothing_for_that_registry(monkeypatch):
    """Two mappings of the REST registry are stored (its telemetry match and
    its name match). The next listing answers the other machine's record and,
    where the matched machine's record was, an item that is not a record:
    absence is not proved, so both mappings stay as they were."""
    world = _world(monkeypatch, historian=True)
    graph = EquipmentVmGraph(("reg-line", "reg-ledger", "hist-x"),
                             assets=["el-qx"]).install(monkeypatch)
    assert onboard._persist_equipment_value_mappings() == 2
    stored = graph.rows()
    assert stored == [("reg-ledger", "N7-QX-210", "hist-x", "Fixture-Lathe-QX-210",
                       "value_format_match"),
                      ("reg-ledger", "N7-QX-210", "reg-line", "LN2-W1", "equipment_name_match")]
    world.connector.records = ["not a record", dict(_LEDGER[1])]
    assert onboard._persist_equipment_value_mappings() == 0
    assert graph.rows() == stored
    assert graph.statements("_RETIRE_STALE_EQUIPMENT_VMS") == []


class _Clock:
    """A fake monotonic clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_a_listing_is_given_a_deadline_and_one_answered_after_it_is_incomplete(monkeypatch):
    """The hook is handed the listing's deadline on the monotonic clock; a
    hook that answers whole but after the deadline is incomplete, so its
    rows write and retire nothing."""
    world = _world(monkeypatch)
    clock = _Clock()
    monkeypatch.setattr(api_registry, "_monotonic", clock, raising=False)
    world.connector.during = lambda: clock.advance(3600.0)
    with onboard.recording_incomplete_reads() as sink:
        api_registry.records("reg-ledger", _REST, _ENTITY)
    (deadline,) = world.connector.deadlines
    assert deadline is not None and 1000.0 < deadline < clock.now
    assert sink == ["over_budget"]


def test_a_listing_inside_its_deadline_is_whole(monkeypatch):
    world = _world(monkeypatch)
    clock = _Clock()
    monkeypatch.setattr(api_registry, "_monotonic", clock, raising=False)
    world.connector.during = lambda: clock.advance(1.0)
    with onboard.recording_incomplete_reads() as sink:
        assert len(api_registry.records("reg-ledger", _REST, _ENTITY)) == len(_LEDGER)
    assert sink == []


# --------------------------------------------------------------------------- #
# the SQL path is unchanged
# --------------------------------------------------------------------------- #

def test_the_sql_pair_and_its_reads_are_unchanged_when_a_rest_registry_joins(monkeypatch):
    """Two SQL registries alone, then with the REST registry beside them: the
    SQL pair's mapping and every statement sent to the SQL schemas are the
    same."""
    alone = _world(monkeypatch, shop=True)
    alone.registries = [r for r in alone.registries if r["sys_id"] != "reg-ledger"]
    before = [vm for vm in onboard._generate_equipment_value_mappings()]
    with_rest = _world(monkeypatch, shop=True)
    after = [vm for vm in onboard._generate_equipment_value_mappings()
             if "reg-ledger" not in (vm["from_system"], vm["to_system"])]
    assert before == after and len(before) == 1
    assert sorted(alone.pg_reads) == sorted(with_rest.pg_reads)
    assert alone.unexpected == [] and with_rest.unexpected == []


# --------------------------------------------------------------------------- #
# persisted, and reconciled like a SQL registry's
# --------------------------------------------------------------------------- #

def test_the_rest_name_match_is_stored_and_retired_when_it_goes(monkeypatch):
    world = _world(monkeypatch)
    graph = EquipmentVmGraph(("reg-line", "reg-ledger"), assets=()).install(monkeypatch)
    assert onboard._persist_equipment_value_mappings() == 1
    assert graph.rows() == [("reg-ledger", "N7-QX-210", "reg-line", "LN2-W1",
                             "equipment_name_match")]
    world.connector.records = [dict(_LEDGER[0], unit_description="Fixture Lathe QX-211")]
    assert onboard._persist_equipment_value_mappings() == 0
    assert graph.rows() == []


# --------------------------------------------------------------------------- #
# size and genericity
# --------------------------------------------------------------------------- #

_CHANGED = ("_readable_registries", "_group_registry_rows", "_registry_table_readable",
            "_pick_registry_field", "_registry_field_density", "_designation_share",
            "_registry_designation_density", "_registry_reference_shaped",
            "_registry_reference_counts", "_equipment_registry_rows", "_equipment_pass")


@pytest.mark.parametrize("name", _CHANGED)
def test_changed_functions_stay_within_50_lines(name):
    assert len(inspect.getsource(getattr(onboard, name)).splitlines()) <= 50, name


def _constants(func) -> list[str]:
    node = ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(node.body[0].value, ast.Constant)) else node.body
    return [c.value for stmt in body for c in ast.walk(stmt)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)]


def test_the_api_registry_code_names_no_vendor_system_or_table():
    funcs = [getattr(onboard, n) for n in _CHANGED]
    funcs += [f for _n, f in inspect.getmembers(api_registry, inspect.isfunction)
              if f.__module__ == api_registry.__name__]
    text = "\n".join(c for f in funcs for c in _constants(f)).lower()
    assert re.findall(r"tulip|i3x|highbyte|cmms|\bmes\b|asset_num|tag_number|"
                      r"cmx|l3-m1", text) == []
