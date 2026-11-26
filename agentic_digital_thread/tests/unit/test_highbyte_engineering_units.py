# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The simulated OPC UA source says which units its machine channels read in.

Rehearsal walk, 2026-10-22: asked for the current state of a mill, the hub
agent's table gave vibration in "inches/second" and the answer printed "in/s",
while the SiteWise models and the workshop pages use mm/s for the same
readings. Nothing the hub serves carried a unit: the OPC UA servers publish
values only, and the hub's ``NameSpace_MCP`` pipeline selects value, path,
name, model and timestamp. The agent filled the gap with a guess.

The source now describes itself. In the datasets the OPC UA servers serve,
every machine (process) node that has a channel with a declared unit carries
one more constant STRING tag, ``<node>_EngineeringUnits``, listing
``<channel>=<unit>`` for each of its own channels with a declared unit (the
dataset writer adds it, ``generate_site_csv``). The units come from one map in
the dataset generator (``ENGINEERING_UNITS``), pinned here to the units the
SiteWise asset models declare (``highbyte/DeploySitewiseModels.py``), the
repo's own source for them. The tag rides the existing paths: ``server.mjs`` serves it like any
STRING tag, the hub's site inputs read the whole branch, and ``NameSpace_MCP``
returns every namespace node (``from: **``). SFC, which may only write a
modelled property, drops it, as it drops the other constant identity tags. The
hub's own SiteWise writer (``NameSpace_to_SiteWise``) copies every process
attribute, so its reshape leaves the units tag out: no SiteWise model has such
a property, and the 28 pre-created models are fixed.

Offline: a live hub import and query still need checking on a deploy.
Literal-free about sites and story assets (the domain-neutrality ratchet
scans tests/).
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agents.demo import sfc_config
from tests.unit import _highbyte_namespace_fixture as hub

_REPO = Path(__file__).resolve().parents[3]
_GENERATOR = _REPO / "highbyte" / "generate_datasets.py"
_MODELS = _REPO / "highbyte" / "DeploySitewiseModels.py"
_HUB_PROJECT = _REPO / "config" / "highbyte" / "intelligencehub-configuration.json"
_SUFFIX = "_EngineeringUnits"


def _load(name: str, path: Path, stub: tuple = ()):
    injected = [m for m in stub if m not in sys.modules]
    for m in injected:
        sys.modules[m] = type(sys)(m)
    try:
        spec = importlib.util.spec_from_file_location(name, str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for m in injected:
            sys.modules.pop(m, None)


@pytest.fixture(scope="module")
def gen():
    return _load("generate_datasets_units_under_test", _GENERATOR)


def _declared_units() -> dict[str, str]:
    """``{property: unit}`` over every SiteWise process model that declares one."""
    models = _load("deploy_sitewise_models_units", _MODELS, stub=("boto3",))
    units: dict[str, str] = {}
    for props in models.PROCESS_MEASUREMENTS.values():
        for prop in props:
            if prop.get("unit"):
                assert units.get(prop["name"], prop["unit"]) == prop["unit"], prop
                units[prop["name"]] = prop["unit"]
    return units


def _written_rows(tmp_path) -> dict[str, list[list[str]]]:
    """``{alias: [type, value, ...]}`` of every row the dataset writer writes
    for a short window, split the way server.mjs splits each row (on ',')."""
    gen = _load("generate_datasets_units_rows", _GENERATOR)
    gen.OUTPUT_DIR = tmp_path
    gen.NUM_STEPS = 12
    gen.CMX441_DOWNTIME = (8, 2)
    gen.make_downtimes = lambda *a, **k: []
    gen.generate_all()
    rows: dict[str, list[list[str]]] = {}
    for path in sorted(tmp_path.glob("*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            for line in handle:
                tokens = line.rstrip("\r\n").split(",")
                if "/" in tokens[0]:
                    rows.setdefault(tokens[0], []).append(tokens)
    return rows


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    return _written_rows(tmp_path_factory.mktemp("units-rows"))


def _nodes(aliases) -> dict[str, list[str]]:
    """``{node path: [channel property, ...]}`` the way server.mjs walks an
    alias: the penultimate segment is the node, the leaf is ``<node>_<prop>``."""
    nodes: dict[str, list[str]] = {}
    for alias in aliases:
        parent, leaf = alias.rsplit("/", 1)
        node = parent.rsplit("/", 1)[-1]
        if leaf.startswith(node + "_"):
            nodes.setdefault(parent, []).append(leaf[len(node) + 1:])
    return nodes


def _parsed(value: str) -> dict[str, str]:
    pairs = [part.split("=", 1) for part in value.split("; ")]
    assert all(len(p) == 2 and p[0] and p[1] for p in pairs), value
    return dict(pairs)


def test_the_generator_units_are_the_units_the_sitewise_models_declare(gen):
    assert gen.ENGINEERING_UNITS == _declared_units()


def test_vibration_and_coolant_temperature_carry_their_declared_units(gen):
    # The walk's finding: vibration is mm/s, the same readings SiteWise holds.
    for axis in ("X", "Y", "Z"):
        assert gen.ENGINEERING_UNITS[f"Vibration_{axis}"] == "mm/s"
    assert gen.ENGINEERING_UNITS["CoolantTemp"] == _declared_units()["CoolantTemp"]


def test_every_node_with_a_declared_unit_channel_lists_exactly_those(rows, gen):
    seen = 0
    for parent, channels in _nodes(rows).items():
        node = parent.rsplit("/", 1)[-1]
        with_unit = [p for p in channels if p in gen.ENGINEERING_UNITS]
        alias = f"{parent}/{node}{_SUFFIX}"
        if not with_unit:
            assert alias not in rows, alias
            continue
        seen += 1
        values = {tuple(tokens[1::4]) for tokens in rows[alias]}
        assert len(values) == 1, f"{alias} is not one constant STRING"
        (dtype, value), = values
        assert dtype == "STRING"
        assert _parsed(value) == {p: gen.ENGINEERING_UNITS[p] for p in with_unit}, alias
    assert seen, "no machine node with a declared-unit channel was found"


def test_the_units_tag_survives_the_server_csv_parse(rows):
    """server.mjs splits each dataset row on ',' and takes the value from the
    sixth field, so a unit list must hold no comma and come back whole."""
    units = [tokens for alias, written in rows.items() if alias.endswith(_SUFFIX)
             for tokens in written]
    assert units
    for tokens in units:
        assert len(tokens) == 6 and tokens[1] == "STRING", tokens
        assert _parsed(tokens[5])


def test_sfc_writes_no_units_channel():
    # SFC writes only modelled properties; the units tag is a description of
    # the node, not a measurement, and must not become a SiteWise property.
    channels = sfc_config.resolve_channels()
    assert channels
    assert not [c for c in channels if c.alias.endswith(_SUFFIX)]


def test_the_namespace_tool_returns_every_node_value():
    # The units tag reaches the agent only because NameSpace_MCP returns the
    # value of every namespace node; pin that query.
    project = json.loads(_HUB_PROJECT.read_text(encoding="utf-8"))
    (pipeline,) = [p for p in project["project"]["pipelines"] if p["name"] == "NameSpace_MCP"]
    (query,) = [s["config"]["query"] for s in pipeline["stages"]
                if s["config"].get("type") == ".SmartQueryConfig"]
    assert query["from"] == ["**"] and "value" in query["select"]["what"]


def test_the_namespace_answer_carries_the_units_beside_the_channels():
    def nodes(tree, out):
        for key, value in tree.items():
            if isinstance(value, dict):
                nodes(value, out)
            elif key.endswith("_Vibration_X"):
                out.append(tree)
        return out
    machines = [node for row in hub.namespace_output() for node in nodes(row, [])]
    assert machines
    for node in machines:
        (units_key,) = [k for k in node if k.endswith(_SUFFIX)]
        listed = _parsed(node[units_key])
        assert listed["Vibration_X"] == "mm/s"


# --------------------------------------------------------------------------- #
# The hub's SiteWise writer never sees the units tag
# --------------------------------------------------------------------------- #
# NameSpace_to_SiteWise reshapes the namespace into the SiteWise payload and
# copies every attribute of a process node; the 28 pre-created SiteWise models
# have no EngineeringUnits property, and writers never change them. The
# standalone highbyte/pipeline_sitewise.json carries the same transform.
_STANDALONE = _REPO / "highbyte" / "pipeline_sitewise.json"


def _reshape(path: Path) -> str:
    project = json.loads(path.read_text(encoding="utf-8"))
    (pipeline,) = [p for p in project["project"]["pipelines"]
                   if p["name"] == "NameSpace_to_SiteWise"]
    (expr,) = [s["config"]["transformExpression"] for s in pipeline["stages"]
               if s["config"].get("type") == ".JavaScriptTransformConfig"]
    return expr


def _run_reshape(expr: str, hierarchy: dict) -> dict:
    """Run the pipeline's reshape stage under node with ``event.value`` set to
    the namespace read (Smart Query ``as: Hierarchy``) and return the value it
    hands the SiteWise write stage."""
    script = (
        f"const EXPR = {json.dumps(expr)};\n"
        "let s = ''; process.stdin.on('data', d => s += d).on('end', () => {\n"
        "  let out; const stage = { setValue(v) { out = v; } };\n"
        "  new Function('event', 'stage', EXPR)({ value: JSON.parse(s) }, stage);\n"
        "  console.log(JSON.stringify(out));\n"
        "});\n")
    done = subprocess.run(["node", "-e", script], input=json.dumps(hierarchy),
                          capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout)


def _without_units(tree):
    if isinstance(tree, dict):
        return {k: _without_units(v) for k, v in tree.items() if not k.endswith(_SUFFIX)}
    return tree


def _payload_keys(payload) -> set:
    out: set = set()

    def walk(node):
        if isinstance(node, dict):
            out.update(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(payload)
    return out


def test_both_copies_of_the_sitewise_reshape_are_the_same():
    assert _reshape(_HUB_PROJECT) == _reshape(_STANDALONE)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_sitewise_writer_payload_leaves_the_units_tag_out():
    """Reproduction: the reshape copied ``<node>_EngineeringUnits`` into each
    CNC process as an ``EngineeringUnits`` attribute, a property no SiteWise
    model has. The tag still reaches NameSpace_MCP (test above)."""
    hierarchy = {hub.company(): hub.site_branches()}
    assert any(k.endswith(_SUFFIX) for k in _payload_keys(hierarchy))
    with_units = _run_reshape(_reshape(_HUB_PROJECT), hierarchy)
    assert "EngineeringUnits" not in _payload_keys(with_units)
    # The tag adds nothing at all to what the writer sends.
    assert with_units == _run_reshape(_reshape(_HUB_PROJECT), _without_units(hierarchy))
    assert _payload_keys(with_units) >= {"Vibration_X", "CoolantTemp"}
