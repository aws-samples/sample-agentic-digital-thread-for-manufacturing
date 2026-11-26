# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The workshop hub's ``NameSpace_MCP`` answer, a LIST of site rows, onboards
through both the wizard and the onboarding chat, offline.

HighByte's Smart Query answers ``as: List``: one object per site node, each
with the node metadata ``_path``, ``_name``, ``_model`` and ``_timestamp``
beside the site's tags (``_highbyte_namespace_fixture``). Flattened row by
row, the metadata names repeat once per site. The wizard's ``POST /map``
joins the tables it is given to the provenance discover stored, one entry per
column name, and refuses a column named twice (``schema_provenance_invalid``).

So the discover answer must name each column once, and then:

* the wizard's map accepts the discover's own tables, its map step keeps
  every mapping on a discovered column, and the provision provenance check
  accepts those mappings;
* the chat's provision check (``onboard_chat_lifecycle._wizard_body``), which
  reads the schema again with the wizard's discover, accepts the same mapped
  fields and leaves none out.

The mapped fields include the repeated metadata columns, the ones a list
answer names once per row.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from fastapi import HTTPException  # noqa: E402

from agents import catalog as catalog_mod  # noqa: E402
from agents import connection_probes, onboard, onboard_agent, server  # noqa: E402
from agents import connection_resolver as cr  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402
from agents.demo import workshop  # noqa: E402
from agents.models import DiscoverRequest, MapRequest, ProvisionRequest  # noqa: E402
from tests.unit import _fake_mcp_hub as fh  # noqa: E402
from tests.unit import _highbyte_namespace_fixture as ns  # noqa: E402

HB_BASE = "https://hb.example.com"
HB_MCP = HB_BASE + "/mcp"
TOOL = "NameSpace_MCP"
#: A tag of each kind the map step proposes, plus the node metadata columns
#: every site row of the list answer repeats.
TAGS = ("Machining.Line1.Line1CNC1.Line1CNC1_Vibration_X",)
MAPPED = (*TAGS, *ns.META)


@pytest.fixture
def hub(monkeypatch):
    hub = fh.FakeHub(accept="bearer", config_tools=True, namespace=ns.namespace_output())
    fh.install(monkeypatch, hub)
    return hub


@pytest.fixture
def workshop_hb(monkeypatch, hub):
    """A fresh catalog holding the workshop's register-only HighByte entry,
    registered the way the startup hook registers it, with the deployment's
    URL and key in the app's env and no graph."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("HIGHBYTE_URL", HB_BASE)
    monkeypatch.setenv("HIGHBYTE_I3X_TOKEN", fh.KEY)
    fresh = SystemCatalog([])
    monkeypatch.setattr(fresh, "_graph_rows", lambda: [])
    for mod in (catalog_mod, onboard, onboard_agent):
        monkeypatch.setattr(mod, "catalog", fresh)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda _sid: None)
    monkeypatch.setattr(cr, "validate_connection_target", lambda *_a, **_k: "203.0.113.10")
    monkeypatch.setattr(connection_probes, "validate_connection_target",
                        lambda *_a, **_k: "203.0.113.10")
    entry = next(s for s in workshop.registered_systems("live", highbyte_shipped=True)
                 if s["id"] == "highbyte")
    from agents.demo.db_routing import demo_pg_dsn
    assert server._register_one_demo_system(
        entry, onboard_agent.register_demo_system, demo_pg_dsn) == "registered"
    return fresh


def _rows() -> list[dict]:
    """The mapping rows a map step proposes for the mapped fields."""
    return [{"field": f"{TOOL}.{column}", "table": TOOL, "concept": "sensor-reading"}
            for column in MAPPED]


def _wizard_discover(cat) -> dict:
    """The wizard's ``POST /discover``, with the body the Onboard view sends for
    the register-only entry (the catalog's empty URL)."""
    conn = (cat.get("highbyte") or {}).get("connection_string") or ""
    body = DiscoverRequest(system_id="highbyte", connection_string=conn,
                           connection_type="MCP Server")
    found = asyncio.run(onboard._onboard_discover_integrity(body))
    assert "error" not in found, found
    return found


def test_the_wizard_maps_and_provisions_the_list_answer(workshop_hb, monkeypatch):
    found = _wizard_discover(workshop_hb)
    started: list = []
    monkeypatch.setattr(onboard, "_run_map_job", lambda *args: started.append(args))
    try:
        accepted = asyncio.run(onboard.onboard_map(
            MapRequest(system_id="highbyte", tables=found["tables"])))
    except HTTPException as exc:
        pytest.fail(f"POST /map refused the discover's own tables: {exc.detail}")
    assert accepted.get("status") == "running", accepted
    ((_job, _sid, trusted),) = [(a[0], a[1], a[2]) for a in started]

    (table,) = found["tables"]
    names = [c["name"] for c in table["columns"]]
    assert table["name"] == TOOL
    assert len(names) == len(set(names)), "a column is named more than once"
    assert set(names) == ns.tag_paths() | set(ns.META)

    joined = onboard._attach_map_provenance(
        {"mappings": _rows()}, onboard._mapping_schema_metadata(trusted))["mappings"]
    assert [m["field"] for m in joined] == [r["field"] for r in _rows()]
    assert {m["schema_revision"] for m in joined} == {found["schema_revision"]}

    body = ProvisionRequest(system_id="highbyte", system_name="HighByte Intelligence Hub",
                            connection_type="MCP Server", connection_string="",
                            mappings=joined)
    trusted_body = onboard._trusted_provision_body(body)
    assert trusted_body.connection_string == HB_MCP
    assert [m["field"] for m in trusted_body.mappings] == [r["field"] for r in _rows()]


def _rows_in_both_forms() -> list[dict]:
    """The same rows as the map model writes them on a live run: a tag path
    without the table in front, the metadata with it."""
    return [{"field": column if not column.startswith("_") else f"{TOOL}.{column}",
             "table": TOOL, "concept": "sensor-reading"} for column in MAPPED]


def test_the_wizard_provisions_tags_mapped_without_the_table(workshop_hb, monkeypatch):
    """The map step writes each mapping it keeps as ``<table>.<column>``, and
    provision takes a tag path mapped without the table in front (a map result
    from before that rewrite) as the same column."""
    found = _wizard_discover(workshop_hb)
    started: list = []
    monkeypatch.setattr(onboard, "_run_map_job", lambda *args: started.append(args))
    asyncio.run(onboard.onboard_map(MapRequest(system_id="highbyte", tables=found["tables"])))
    ((_job, _sid, trusted),) = [(a[0], a[1], a[2]) for a in started]
    assert any("." in r["field"] and not r["field"].startswith(TOOL + ".")
               for r in _rows_in_both_forms())

    joined = onboard._attach_map_provenance(
        {"mappings": _rows_in_both_forms()}, onboard._mapping_schema_metadata(trusted))["mappings"]
    assert [m["field"] for m in joined] == [r["field"] for r in _rows()]
    body = ProvisionRequest(system_id="highbyte", system_name="HighByte Intelligence Hub",
                            connection_type="MCP Server", connection_string="",
                            mappings=joined)
    assert [m["field"] for m in onboard._trusted_provision_body(body).mappings] == [
        r["field"] for r in _rows()]

    as_written = [{**r, "schema_revision": found["schema_revision"]}
                  for r in _rows_in_both_forms()]
    body = body.model_copy(update={"mappings": as_written})
    assert [m["field"] for m in onboard._trusted_provision_body(body).mappings] == [
        r["field"] for r in _rows()]


def test_an_identity_rule_on_a_hub_tag_names_the_whole_tag(monkeypatch):
    """The hub's sites repeat tag names (an order-id tag ends ``Line1_OrderID``
    in more than one area). A confirmed rule on one of them is written on that
    whole tag path, and a rule naming only the shared name is rejected."""
    tags = sorted(p for p in ns.tag_paths() if p.endswith(".Line1_OrderID"))
    assert len(tags) > 1, "the fixture no longer repeats the tag name"
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda c, p=None: [{"tbl": "orders"}] if "HAS_FIELD" in c else [])

    class _Catalog:
        def get(self, _sid):
            return None

        def get_onboarded_ids(self):
            return {"erp"}
    monkeypatch.setattr(onboard, "catalog", _Catalog())
    rule = {"concept": "work-order", "source_field": f"{TOOL}.{tags[-1]}", "source_pattern": "",
            "target_system": "erp", "target_field": "orders.order_ref", "target_pattern": ""}
    ctx = onboard._ProvisionState(
        body=type("B", (), {"value_mappings": []})(), system_id="highbyte",
        system_name="HighByte Intelligence Hub", plant="", schema=HB_MCP,
        rules=[rule, {**rule, "source_field": "Line1_OrderID"}],
        mappings=[{"table": TOOL, "field": f"{TOOL}.{t}", "concept": "work-order"} for t in tags],
        connection_type="MCP Server", cm_items=[], committable=set())
    onboard._dp_identity_rules(ctx)
    onboard._dp_rule_joinpaths(ctx)
    irs = [p for c, p in writes if c.startswith("MERGE (ir:IdentityRule")]
    assert [(p["src_field"], p["src_tbl"]) for p in irs] == [(tags[-1], TOOL)]
    assert [p["ffld"] for c, p in writes if c.startswith("MERGE (jp:JoinPath")] == [tags[-1]]
    assert [r["rule"]["source_field"] for r in ctx.rules_rejected] == ["Line1_OrderID"]


def test_the_chat_provision_accepts_the_same_mapped_fields(workshop_hb):
    from agents import onboard_chat_lifecycle as lifecycle
    chat = onboard_agent._discover_backend_result(
        "highbyte", workshop_hb.get("highbyte"), "MCP Server", {})
    assert "error" not in chat, chat
    session = {"discovered_tables": chat["tables"], "mappings": _rows(), "rules": []}
    out = lifecycle._wizard_body("highbyte", session)
    assert isinstance(out, tuple), out
    body, left_out = out
    assert left_out == {}
    assert body.connection_string == HB_MCP
    assert [m["field"] for m in body.mappings] == [r["field"] for r in _rows()]
    assert all(m["schema_revision"] for m in body.mappings)
