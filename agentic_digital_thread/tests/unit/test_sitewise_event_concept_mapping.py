# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 3.1 — end-to-end (offline) validation that SiteWise event/metric model
PROPERTIES flow through provision into the graph as
``System-[:HAS_SCHEMA]->SchemaEntry(model)-[:HAS_FIELD]->Field(property)-[:MAPS_TO]->Concept``
with the intended concepts + roles (ADR-0037), and that ``get_system_schema`` then
reads those Fields+Concepts back.

Fully synthetic: SiteWise pseudo-tables use ARBITRARY model/property names (never
a live customer's names). No AWS/Neptune/LLM — ``neptune_write`` is captured, the map step is
supplied directly as ``body.mappings`` (the shape ``_run_map_job`` produces).
"""
import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import json  # noqa: E402

import agents.connection_resolver as cr  # noqa: E402
import agents.graph_tools as graph_tools  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402


class _FakeAgentManager:
    def __init__(self):
        self._agents = {}

    def start_agent(self, **kwargs):
        return "mcp://test-agent"

    def get_mcp_url(self, system_id):
        return "mcp://test-agent"


class _FakeCatalog:
    def __init__(self, entry):
        self._entry = entry

    def get(self, system_id):
        return self._entry if system_id == self._entry.get("id") else None

    def update_status(self, system_id, status):
        pass

    def get_onboarded_ids(self):
        return set()


# SYNTHETIC SiteWise pseudo-tables (one per asset model) with the intended concept map.
# Property NAMES are arbitrary — never a live customer's names.
_SYSTEM_ID = "sw-synth"
_MAPPINGS = [
    # evt_model — a machine-state event stream
    {"field": "evt_model.machine_ref", "table": "evt_model", "concept": "equipment-id",
     "confidence": 0.9, "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9}]},
    {"field": "evt_model.dur", "table": "evt_model", "concept": "downtime",
     "confidence": 0.8, "mappings": [{"concept": "downtime", "role": "measure", "confidence": 0.8}]},
    {"field": "evt_model.from_st", "table": "evt_model", "concept": "machine-state",
     "confidence": 0.85, "mappings": [{"concept": "machine-state", "role": "correlate", "confidence": 0.85}]},
    {"field": "evt_model.to_st", "table": "evt_model", "concept": "machine-state",
     "confidence": 0.85, "mappings": [{"concept": "machine-state", "role": "correlate", "confidence": 0.85}]},
    {"field": "evt_model.reason", "table": "evt_model", "concept": "failure-mode",
     "confidence": 0.8, "mappings": [{"concept": "failure-mode", "role": "correlate", "confidence": 0.8}]},
    {"field": "evt_model.ts", "table": "evt_model", "concept": "event-time",
     "confidence": 0.9, "mappings": [{"concept": "event-time", "role": "measure", "confidence": 0.9}]},
    # metric_model — produced-output / speed metrics
    {"field": "metric_model.amt", "table": "metric_model", "concept": "throughput",
     "confidence": 0.85, "mappings": [{"concept": "throughput", "role": "measure", "confidence": 0.85}]},
    {"field": "metric_model.waste", "table": "metric_model", "concept": "scrap-waste",
     "confidence": 0.85, "mappings": [{"concept": "scrap-waste", "role": "measure", "confidence": 0.85}]},
    {"field": "metric_model.spd", "table": "metric_model", "concept": "machine-speed",
     "confidence": 0.8, "mappings": [{"concept": "machine-speed", "role": "measure", "confidence": 0.8}]},
]

_EXPECTED = {
    "evt_model.machine_ref": ("equipment-id", "identifier"),
    "evt_model.dur": ("downtime", "measure"),
    "evt_model.from_st": ("machine-state", "correlate"),
    "evt_model.to_st": ("machine-state", "correlate"),
    "evt_model.reason": ("failure-mode", "correlate"),
    "evt_model.ts": ("event-time", "measure"),
    "metric_model.amt": ("throughput", "measure"),
    "metric_model.waste": ("scrap-waste", "measure"),
    "metric_model.spd": ("machine-speed", "measure"),
}


def _run_provision_capturing_writes():
    """Run ``_do_provision`` for a SiteWise (Direct) system with mappings; capture
    every ``neptune_write`` (cypher, params). Returns the captured write list."""
    writes = []

    entry = {
        "id": _SYSTEM_ID, "name": "SW Synth", "system_type": "Historian",
        "connection_type": "SiteWise (Direct)", "schema": "", "status": "available",
    }

    saved = {
        "cr_write_connection": cr.write_connection,
        "neptune_write": onboard.neptune_write,
        "neptune_query": onboard.neptune_query,
        "bedrock_call": onboard.bedrock_call,
        "catalog": onboard.catalog,
        "agent_manager": onboard._agent_manager,
        "add_system_agent": onboard.add_system_agent,
        "detect_joins": onboard._detect_internal_joinpaths,
        "mqtt_start": onboard.mqtt_subscriber.start_subscriber,
    }
    import agents.model_config as model_config
    import agents.self_learning as self_learning
    import boto3
    saved_models = model_config.get_configured_models
    saved_accepted = self_learning.get_accepted_for_system
    saved_boto = boto3.client
    try:
        cr.write_connection = lambda *a, **k: None
        onboard.neptune_write = lambda cy, params=None: writes.append((cy, params or {}))
        onboard.neptune_query = lambda *a, **k: []
        onboard.bedrock_call = lambda *a, **k: "generated prompt"
        onboard.catalog = _FakeCatalog(entry)
        onboard._agent_manager = _FakeAgentManager()
        onboard.add_system_agent = lambda *a, **k: None
        onboard._detect_internal_joinpaths = lambda *a, **k: []
        onboard.mqtt_subscriber.start_subscriber = lambda *a, **k: None
        model_config.get_configured_models = lambda: {"agent": "test-agent-model", "orchestrator": "test"}
        self_learning.get_accepted_for_system = lambda *a, **k: []

        def _boto_raises(*a, **k):
            raise RuntimeError("boto3 disabled in unit test")
        boto3.client = _boto_raises

        body = ProvisionRequest(
            system_id=_SYSTEM_ID, system_name="SW Synth", plant="All Sites",
            schema="", connection_type="SiteWise (Direct)", mappings=_MAPPINGS,
        )
        result = onboard._do_provision(body)
        return result, writes
    finally:
        cr.write_connection = saved["cr_write_connection"]
        onboard.neptune_write = saved["neptune_write"]
        onboard.neptune_query = saved["neptune_query"]
        onboard.bedrock_call = saved["bedrock_call"]
        onboard.catalog = saved["catalog"]
        onboard._agent_manager = saved["agent_manager"]
        onboard.add_system_agent = saved["add_system_agent"]
        onboard._detect_internal_joinpaths = saved["detect_joins"]
        onboard.mqtt_subscriber.start_subscriber = saved["mqtt_start"]
        model_config.get_configured_models = saved_models
        self_learning.get_accepted_for_system = saved_accepted
        boto3.client = saved_boto


def _maps_to_edges(writes):
    """Extract {field_id: (concept, role)} from captured MAPS_TO writes."""
    edges = {}
    for cy, params in writes:
        if "MERGE (f)-[r:MAPS_TO]->(c)" in cy and "fid" in params and "cid" in params:
            edges[params["fid"]] = (params["cid"], params.get("role"))
    return edges


def test_provision_writes_schema_entries_per_model():
    result, writes = _run_provision_capturing_writes()
    assert result.get("status") == "provisioned", result
    schema_names = {
        params.get("name")
        for cy, params in writes
        if "MERGE (se:SchemaEntry" in cy and params.get("name")
    }
    assert "evt_model" in schema_names
    assert "metric_model" in schema_names


def test_provision_writes_fields_per_property():
    _result, writes = _run_provision_capturing_writes()
    field_ids = {
        params.get("fid")
        for cy, params in writes
        if "MERGE (f:Field {id: $fid})" in cy and params.get("fid")
    }
    for col in _EXPECTED:
        assert f"{_SYSTEM_ID}.{col}" in field_ids, f"missing Field for {col}"


def test_provision_writes_maps_to_with_expected_concept_and_role():
    _result, writes = _run_provision_capturing_writes()
    edges = _maps_to_edges(writes)
    for col, (concept, role) in _EXPECTED.items():
        fid = f"{_SYSTEM_ID}.{col}"
        assert fid in edges, f"no MAPS_TO edge for {col}"
        got_concept, got_role = edges[fid]
        assert got_concept == concept, f"{col}: concept {got_concept} != {concept}"
        assert got_role == role, f"{col}: role {got_role} != {role}"


def test_equipment_id_identifier_present_for_cross_system_mapping():
    """An equipment-id identifier must exist so cross-system value_mappings attach."""
    _result, writes = _run_provision_capturing_writes()
    edges = _maps_to_edges(writes)
    ids = [(fid, c, r) for fid, (c, r) in edges.items() if c == "equipment-id" and r == "identifier"]
    assert ids, "no equipment-id identifier written"


def test_get_system_schema_reads_back_properties_and_concepts(monkeypatch):
    """graph_tools.get_system_schema surfaces the written Fields + concept map so the
    Mode A agent bakes them into tables_desc."""
    _result, writes = _run_provision_capturing_writes()
    edges = _maps_to_edges(writes)

    # Build the rows get_system_schema's query would return from the captured writes.
    rows = []
    for fid, (concept, role) in edges.items():
        # fid == "<system>.<table>.<column>"
        _sid, table, field = fid.split(".", 2)
        rows.append({
            "table_name": table, "field_name": field,
            "concept": concept, "role": role,
            "disambiguation": None, "is_partition": None, "data_type": "sitewise property",
        })

    monkeypatch.setattr(graph_tools, "neptune_query", lambda *a, **k: rows)
    out = json.loads(graph_tools.get_system_schema(_SYSTEM_ID))

    assert "evt_model" in out and "metric_model" in out
    assert "from_st" in out["evt_model"]["columns"]
    assert out["evt_model"]["concept_map"]["from_st"]["concept"] == "machine-state"
    assert out["metric_model"]["concept_map"]["amt"]["concept"] == "throughput"
    assert out["metric_model"]["concept_map"]["spd"]["concept"] == "machine-speed"
