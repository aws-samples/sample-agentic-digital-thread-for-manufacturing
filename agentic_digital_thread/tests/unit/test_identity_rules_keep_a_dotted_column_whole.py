# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An identity rule on a dotted column names the whole column.

An MCP namespace's columns are dotted tag paths, and one last segment
(``Line1_OrderID``) can end tags in more than one area. Provision writes a
rule's source column on its IdentityRule (``source_field``) and its JoinPath
(``from_field``), and the resolve step samples the column a rule is built
from. For a dotted column that is the whole column, with its table, so the
rule points at one tag; a rule that names a tag only by a part two columns
share is rejected, not written to the shared part. A rule on a single-dot
column is written exactly as before.

The phase helpers run on stubbed graph writes; the end-to-end test runs the
wizard's discover and provision on the fakes of
test_chat_provision_is_the_wizard_provision.py.
"""
from __future__ import annotations

import asyncio
import copy
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents import connection_types as ct  # noqa: E402
from agents.models import DiscoverRequest, ProvisionRequest  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    JOB_WAIT, SID, World, _wait_for)

TABLE = "NameSpace_MCP"
SCHEMA = "https://mcp.example.com/mcp"
TARGET = "target-sys"
A_ORDER = "AreaA.Line1.Line1_OrderID"
B_ORDER = "AreaB.Line1.Line1_OrderID"
COLUMNS = {A_ORDER: "str", B_ORDER: "str", "AreaA.Line1.Line1_Output": "int", "_timestamp": "str"}


class _Catalog:
    def __init__(self, entries=None):
        self._entries = entries or {}

    def get(self, system_id):
        return self._entries.get(system_id)

    def get_onboarded_ids(self):
        return {TARGET}


@pytest.fixture
def writes(monkeypatch):
    """Every graph write of the c/c2 phases; the target's field is in table ``orders``."""
    recorded: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda c, p=None: [{"tbl": "orders"}] if "HAS_FIELD" in c else [])
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "catalog", _Catalog())
    return recorded


def _mapping(column: str, table: str = TABLE) -> dict:
    return {"table": table, "field": f"{table}.{column}", "concept": "order-id",
            "mappings": [{"concept": "order-id", "role": "identifier", "confidence": 0.9}]}


def _rule(source_field: str, **over) -> dict:
    rule = {"concept": "order-id", "source_field": source_field, "source_pattern": "",
            "target_system": TARGET, "target_field": "orders.order_ref",
            "target_pattern": "", "confidence": 0.9}
    rule.update(over)
    return rule


def _ctx(rules, mappings, connection_type=ct.MCP_SERVER, schema=SCHEMA):
    return onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id="ns-sys", system_name="Namespace",
        plant="", schema=schema, rules=rules, mappings=mappings,
        connection_type=connection_type, cm_items=[], committable=set())


def _run(rules, mappings, **kw):
    ctx = _ctx(rules, mappings, **kw)
    onboard._dp_identity_rules(ctx)
    onboard._dp_rule_joinpaths(ctx)
    return ctx


def _of(recorded, prefix):
    return [p for c, p in recorded if c.startswith(prefix)]


def _identity_rules(recorded):
    return _of(recorded, "MERGE (ir:IdentityRule")


def _join_paths(recorded):
    return _of(recorded, "MERGE (jp:JoinPath")


NAMESPACE_MAPPINGS = [_mapping(c) for c in COLUMNS]


# ── provision writes the whole column ──────────────────────────────────────

@pytest.mark.parametrize("source_field,source_table", [
    (f"{TABLE}.{B_ORDER}", TABLE),
    (f"{TABLE}.{B_ORDER}", None),
    (B_ORDER, TABLE),
    (B_ORDER, None),
])
def test_a_rule_on_a_dotted_column_writes_the_whole_column(writes, source_field, source_table):
    over = {"source_table": source_table} if source_table else {}
    ctx = _run([_rule(source_field, **over)], copy.deepcopy(NAMESPACE_MAPPINGS))
    assert ctx.rules_rejected == []
    (ir,) = _identity_rules(writes)
    assert ir["src_field"] == B_ORDER
    assert ir["src_tbl"] == TABLE
    (jp,) = _join_paths(writes)
    assert (jp["ftbl"], jp["ffld"]) == (f"{SCHEMA}.{TABLE}", B_ORDER)
    assert jp["desc"].startswith(f"Namespace {TABLE}.{B_ORDER} -> ")


def test_two_rules_on_columns_with_one_leaf_are_two_rules(writes):
    """Rules on two tag paths that end the same way are two rules: two ids,
    two JoinPaths, and neither counts as a second target of the other."""
    _run([_rule(f"{TABLE}.{A_ORDER}"), _rule(f"{TABLE}.{B_ORDER}")],
         copy.deepcopy(NAMESPACE_MAPPINGS))
    irs = _identity_rules(writes)
    assert sorted(ir["src_field"] for ir in irs) == [A_ORDER, B_ORDER]
    assert len({ir["rid"] for ir in irs}) == 2
    assert {ir["conf"] for ir in irs} == {0.7}
    jps = _join_paths(writes)
    assert sorted(jp["ffld"] for jp in jps) == [A_ORDER, B_ORDER]
    assert len({jp["jpid"] for jp in jps}) == 2


@pytest.mark.parametrize("source_field", ["Line1_OrderID", "Line1.Line1_OrderID",
                                          f"{TABLE}.Line1_OrderID"])
def test_a_rule_naming_a_part_two_columns_share_is_rejected(writes, source_field):
    ctx = _run([_rule(source_field)], copy.deepcopy(NAMESPACE_MAPPINGS))
    assert _identity_rules(writes) == [] and _join_paths(writes) == []
    (rejected,) = ctx.rules_rejected
    assert "more than one" in rejected["reason"]


def test_a_rule_naming_a_unique_tail_of_a_dotted_column_takes_that_column(writes):
    _run([_rule("Line1_Output")], copy.deepcopy(NAMESPACE_MAPPINGS))
    (ir,) = _identity_rules(writes)
    assert (ir["src_field"], ir["src_tbl"]) == ("AreaA.Line1.Line1_Output", TABLE)


# ── resolve samples the whole column ───────────────────────────────────────

def test_resolve_samples_the_whole_dotted_column(monkeypatch):
    sampled: list = []
    monkeypatch.setattr(onboard, "catalog", _Catalog({"ns-sys": {
        "connection_type": ct.MCP_SERVER, "schema": SCHEMA}}))
    import agents.resolve_evidence as resolve_evidence
    monkeypatch.setattr(resolve_evidence, "sample_field_values",
                        lambda **kw: sampled.append((kw["table"], kw["field"])) or [])
    rows = [_mapping(A_ORDER), _mapping(B_ORDER), _mapping("order_no", table="orders"),
            {"table": "orders", "field": "line_no", "concept": "order-id"}]
    fields = onboard._affinity_new_fields("ns-sys", rows)
    assert sampled == [(TABLE, A_ORDER), (TABLE, B_ORDER), ("orders", "order_no"),
                       ("orders", "line_no")]
    assert [f.qualified for f in fields] == [f"{TABLE}.{A_ORDER}", f"{TABLE}.{B_ORDER}",
                                             "orders.order_no", "orders.line_no"]


# ── a single-dot column is written exactly as before ───────────────────────

SINGLE_DOT_MAPPINGS = [_mapping("wo_number", table="work_orders"),
                       _mapping("wo_number", table="orders"),
                       _mapping("lot_code", table="lots")]
SINGLE_DOT_RULES = [
    _rule("work_orders.wo_number", source_table="work_orders"),
    _rule("wo_number"),
    _rule("mes.lots.lot_code", target_field="lot_ref", concept="lot-id"),
    _rule("not_mapped_col", source_table="lots"),
]

#: What provision wrote for SINGLE_DOT_RULES before dotted columns were read
#: whole (captured on the code that read every source column by its last
#: segment); the same for every connection type below.
SINGLE_DOT_IDENTITY_RULES = [
    ("ir-ns-sys-431dd2c97f6269be", "wo_number", 0.7),
    ("ir-ns-sys-431dd2c97f6269be", "wo_number", 0.7),
    ("ir-ns-sys-26ae991096f05cd0", "lot_code", 0.7),
    ("ir-ns-sys-4210ca1ca92de666", "not_mapped_col", 0.7),
]
SINGLE_DOT_JOIN_PATHS = [
    ("jp-ns-sys-431dd2c97f6269be", "db.work_orders", "wo_number"),
    ("jp-ns-sys-431dd2c97f6269be", "db.work_orders", "wo_number"),
    ("jp-ns-sys-26ae991096f05cd0", "db.lots", "lot_code"),
    ("jp-ns-sys-4210ca1ca92de666", "db.lots", "not_mapped_col"),
]


@pytest.mark.parametrize("connection_type", [
    "PostgreSQL", ct.ATHENA, ct.SITEWISE_I3X, ct.SITEWISE_DIRECT, ct.REST_API, ct.TULIP])
def test_a_single_dot_rule_is_written_exactly_as_before(writes, connection_type):
    ctx = _run(copy.deepcopy(SINGLE_DOT_RULES), copy.deepcopy(SINGLE_DOT_MAPPINGS),
               connection_type=connection_type, schema="db")
    assert ctx.rules_rejected == []
    assert [(p["rid"], p["src_field"], p["conf"]) for p in _identity_rules(writes)] == \
        SINGLE_DOT_IDENTITY_RULES
    assert all("src_tbl" not in p for p in _identity_rules(writes))
    assert all("source_table" not in c for c, _p in writes if "IdentityRule" in c)
    assert [(p["jpid"], p["ftbl"], p["ffld"]) for p in _join_paths(writes)] == \
        SINGLE_DOT_JOIN_PATHS


# ── end to end: the wizard provisions a confirmed rule ─────────────────────

def test_the_wizard_provisions_a_confirmed_rule_on_a_dotted_column(monkeypatch):
    world = World(monkeypatch)
    world.discover_tables = [{"name": TABLE, "row_count": len(COLUMNS), "columns": [
        {"name": c, "data_type": t} for c, t in COLUMNS.items()]}]
    world.catalog.add({"id": TARGET, "name": "Target", "status": "onboarded",
                       "connection_type": ct.REST_API, "schema": "", "system_type": "MES"})
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda c, p=None: [{"tbl": "orders"}] if "HAS_FIELD" in c else [])
    entry = world.catalog.get(SID)
    disc = asyncio.run(onboard.onboard_discover(DiscoverRequest(
        system_id=SID, connection_string=entry["connection_string"],
        connection_type=entry["connection_type"], connection_config={})))
    revision = disc["tables"][0]["schema_revision"]
    mappings = [{**_mapping(c), "data_type": t, "relation_kind": "unknown",
                 "schema_revision": revision} for c, t in COLUMNS.items()]
    started = asyncio.run(onboard.onboard_provision(ProvisionRequest(
        system_id=SID, system_name=entry["name"], plant=entry["plant"],
        schema=disc.get("schema_name") or "", mappings=mappings,
        rules=[_rule(f"{TABLE}.{B_ORDER}", source_table=TABLE)],
        connection_type=entry["connection_type"], connection_string=entry["connection_string"],
        connection_config={})))
    done = _wait_for(lambda: (lambda r: r if r.get("status") != "running" else None)(
        asyncio.run(onboard.onboard_provision_status(started["job_id"]))), JOB_WAIT)
    assert done.get("status") == "done", done
    assert done.get("identity_rules_created") == 1, done
    irs = [p for c, p in world.writes if c.startswith("MERGE (ir:IdentityRule")]
    assert [(p["src_field"], p["src_tbl"]) for p in irs] == [(B_ORDER, TABLE)]
    jps = [p for c, p in world.writes if c.startswith("MERGE (jp:JoinPath")
           and p.get("jpid", "").startswith(f"jp-{SID}-")]
    assert [(p["ftbl"], p["ffld"]) for p in jps] == [(f"{disc['schema_name']}.{TABLE}", B_ORDER)]


# ── the entity resolver reads a rule on a dotted column ────────────────────

def _resolver(monkeypatch, rule_rows, queried):
    import agents.resolve as resolve
    monkeypatch.setattr(resolve, "neptune_query",
                        lambda c, p=None: copy.deepcopy(rule_rows) if "IdentityRule" in c else [])
    monkeypatch.setattr(resolve, "get_schema", lambda sid: {"ns-sys": SCHEMA}.get(sid, "db"))
    monkeypatch.setattr(resolve, "get_name", lambda sid: sid)
    monkeypatch.setattr(resolve, "catalog", _Catalog({"ns-sys": {"connection_type": ct.MCP_SERVER},
                                                      TARGET: {"connection_type": "PostgreSQL"}}))
    monkeypatch.setattr(resolve, "query_system",
                        lambda sid, sql, params=None: queried.append((sid, sql)) or {"rows": [],
                                                                                    "columns": []})
    monkeypatch.setattr(resolve, "pg", lambda *a, **k: queried.append(a) or {"rows": [],
                                                                             "columns": []})
    return resolve


def test_the_entity_resolver_passes_over_a_rule_on_a_dotted_column(monkeypatch):
    """A tag path is not a SQL column, so the resolver's direct lookup leaves a
    rule on one alone instead of failing the whole resolve; a single-dot rule
    is looked up as before."""
    rows = [{"id": "ir-1", "src_sys": "ns-sys", "src_field": B_ORDER, "src_pat": "",
             "tgt_sys": TARGET, "tgt_field": "order_ref", "tgt_pat": "", "conf": 0.7},
            {"id": "ir-2", "src_sys": "ns-sys", "src_field": "order_no", "src_pat": "",
             "tgt_sys": TARGET, "tgt_field": "order_ref", "tgt_pat": "", "conf": 0.7}]
    queried: list = []
    resolve = _resolver(monkeypatch, rows, queried)
    raw, resolution, unified, *_ = resolve.resolve_entity("order-id", "4711")
    assert ("ns-sys", "SELECT * FROM order_no LIMIT 1") in queried
    assert not any(B_ORDER in str(q) for q in queried)
    assert raw == [] and resolution == [] and unified == []
