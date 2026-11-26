# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What onboarding the workshop's HighByte hub adds to the graph, offline.

Module 7 counts the graph's node types after module 6, so this pins the parts
of HighByte onboarding that are deterministic, on the ``NameSpace_MCP`` output
the workshop hub returns (``_highbyte_namespace_fixture``: built from the OPC UA
datasets and HighByte's documented Smart Query List shape):

* discovery lists one table, ``NameSpace_MCP``, whose columns are every tag
  path below a site (843 across the four sites: 837 channels and status tags
  plus six machines' ``_EngineeringUnits`` tags) plus the four node-metadata
  columns, each once;
* map keeps one row per discovered column, and provision writes one ``Field``
  per row, so the hub adds 1 ``System``, 1 ``SchemaEntry`` and 847 ``Field``
  nodes; ``Asset``/``AssetModel``/``Property`` are written only for SiteWise;
* the deterministic join passes write nothing for the hub: an MCP Server
  system's field values are never sampled and cannot be probed, so the
  identifier-core and exact-agreement passes find no ``IdentityRule`` or
  ``JoinPath`` with it, in either direction, even where the hub's values equal
  a SiteWise asset name.

Which concept each field maps to, and any identity rule or value mapping the
resolver proposes, are model output the attendee confirms at the checkpoints;
those are test-event checks, not pinned here.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from agents import identity_affinity as ia  # noqa: E402
from agents import onboard, onboard_agent  # noqa: E402
from agents import resolve_evidence  # noqa: E402
from tests.unit import _fake_mcp_hub as fh  # noqa: E402
from tests.unit import _highbyte_namespace_fixture as ns  # noqa: E402

HB_MCP = "https://hb.example.com/mcp"
TAGS = 843
COLUMNS = TAGS + len(ns.META)


@pytest.fixture
def tables(monkeypatch):
    hub = fh.FakeHub(accept="bearer", config_tools=True, namespace=ns.namespace_output())
    fh.install(monkeypatch, hub)
    found = onboard_agent._discover_mcp(onboard._mcp_entry(HB_MCP, fh.KEY, "bearer"),
                                        system_id="highbyte")
    assert "error" not in found, found
    return found["tables"]


def test_the_fixture_has_every_dataset_tag():
    assert len(ns.tag_paths()) == TAGS
    assert {path.split(".")[0] for path in ns.tag_paths()} == {
        "AutoParts", "Bottling", "FoodProcessing", "Machining"}


def test_discovery_lists_each_tag_path_and_each_metadata_column_once(tables):
    assert [t["name"] for t in tables] == ["NameSpace_MCP"]
    columns = {c["name"] for c in tables[0]["columns"]}
    assert columns == ns.tag_paths() | set(ns.META)
    assert len(columns) == COLUMNS
    assert "Machining.Line1.Line1CNC1.Line1CNC1_Vibration_X" in columns


def test_the_discover_summary_counts_each_tag_once_and_metadata_apart(monkeypatch):
    """The message the Onboard view shows after Discover: the tag paths it
    reports are the tags below the sites (843), each counted once, and the
    per-area counts add up to that total. The node metadata columns repeat in
    every site object, so they are counted apart (4)."""
    import asyncio
    import re
    from agents import connection_resolver as cr
    hub = fh.FakeHub(accept="bearer", config_tools=True, namespace=ns.namespace_output())
    fh.install(monkeypatch, hub)
    monkeypatch.setenv("HIGHBYTE_URL", HB_MCP.removesuffix("/mcp"))
    monkeypatch.setenv("HIGHBYTE_I3X_TOKEN", fh.KEY)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda _sid: None)
    result = asyncio.run(onboard._discover_mcp_server(None, "highbyte", "", "", "MCP Server"))
    summary = result["ai_analysis"]
    assert f"**{TAGS} total tag paths**" in summary, summary
    assert f"{len(ns.META)} node metadata fields" in summary, summary
    per_area = [int(n) for n in re.findall(r", (\d+) tags$", summary, flags=re.M)]
    assert len(per_area) == 4 and sum(per_area) == TAGS, summary


def test_provision_writes_one_field_per_discovered_column(tables, monkeypatch):
    """Map keeps a row for every discovered column (``_reconcile_map_coverage``
    synthesizes a needs-review row for any the mapper leaves out); provision
    writes a Field per row. With no concept on a row, no MAPS_TO edge."""
    monkeypatch.setattr(onboard, "_requeue_missing_columns", lambda *a, **k: [])  # the mapper returns nothing
    rows = onboard._reconcile_map_coverage("highbyte", tables, [], False, [])
    assert len(rows) == COLUMNS

    writes = []
    monkeypatch.setattr(onboard, "neptune_write", lambda q, p=None: writes.append((q, p)))
    ctx = onboard._ProvisionState(
        body=None, system_id="highbyte", system_name="HighByte", plant="", schema="",
        rules=[], mappings=rows, connection_type="MCP Server", cm_items=[])
    ctx.default_table = "highbyte"
    for row in rows:
        onboard._dp_field_row(ctx, row)
    fields = {p["fid"] for q, p in writes if q.startswith("MERGE (f:Field")}
    assert len(fields) == COLUMNS
    assert all(fid.startswith("highbyte.NameSpace_MCP.") for fid in fields)
    assert not [q for q, _p in writes if "MAPS_TO" in q]


def test_the_hub_fields_are_never_sampled_or_probed(tables):
    field = sorted(ns.tag_paths())[0]
    assert resolve_evidence.sample_field_values(
        system_id="highbyte", connection_type="MCP Server", schema="",
        table="NameSpace_MCP", field=field, connection_config={}) == []
    assert resolve_evidence.probe_field_values(
        system_id="highbyte", connection_type="MCP Server", schema="",
        table="NameSpace_MCP", field=field, values=["CMX-441"]) is None


def test_the_join_passes_find_no_rule_with_the_hub_either_way():
    """Even with a SiteWise asset-name sample that equals a hub value."""
    hub_field = ia.SampledField("highbyte", "NameSpace_MCP", "_name", "equipment-id",
                                tuple(), concepts=frozenset({"equipment-id"}))
    site = sorted(ns.site_branches())[0]  # a value the hub's _name column holds
    sitewise = ia.SampledField("sitewise-direct", "assets", "asset_name", "equipment-id",
                               (site,) * 40, concepts=frozenset({"equipment-id"}))

    def probe(_field, values):
        return None  # what probe_field_values answers for an MCP Server system

    assert ia.find_affinity_rules([hub_field], [sitewise], probe) == []
    assert ia.find_affinity_rules([sitewise], [hub_field], probe) == []
