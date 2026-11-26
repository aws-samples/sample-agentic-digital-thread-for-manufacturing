# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for emergent-concept mapping on telemetry-rich sources (Task A3).

The ONE ``_is_telemetry_rich`` helper gates all three concept sites:
(1) the map-step CORE_CONCEPTS filter bypass,
(2) the unmatched-fields default to 'sensor-reading',
(3) the provision-time concept write bypass (emergent non-CORE Concept nodes).
Plus the NEW CONCEPTS map-prompt block, injected ONLY for telemetry-rich
systems.

'SiteWise (i3X)' now behaves like 'MCP Server' at every site; SQL/relational
systems keep the curated CORE_CONCEPTS filtering (regression-pinned here).
All fixtures are synthetic — no dataset/demo literals.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402


# --------------------------------------------------------------------------- #
# _is_telemetry_rich truth table
# --------------------------------------------------------------------------- #
def test_is_telemetry_rich_truth_table():
    assert onboard._is_telemetry_rich("MCP Server") is True
    assert onboard._is_telemetry_rich("SiteWise (i3X)") is True
    assert onboard._is_telemetry_rich("PostgreSQL") is False
    assert onboard._is_telemetry_rich("SiteWise (Direct)") is False
    assert onboard._is_telemetry_rich("REST API") is False
    assert onboard._is_telemetry_rich("Athena") is False
    assert onboard._is_telemetry_rich(None) is False
    assert onboard._is_telemetry_rich("") is False


# --------------------------------------------------------------------------- #
# Map prompt: NEW CONCEPTS block only for telemetry-rich sources
# --------------------------------------------------------------------------- #
def test_map_prompt_new_concepts_block_for_telemetry_rich():
    prompt = onboard._build_map_prompt("sys-a", "equipment-id, sensor-reading",
                                       "  tags: spindle_speed",
                                       telemetry_rich=True)
    assert "NEW CONCEPTS" in prompt
    assert "telemetry-rich source" in prompt
    assert "'vibration'" in prompt


def test_map_prompt_unchanged_for_sql_systems():
    with_flag = onboard._build_map_prompt("sys-a", "c1", "f1", telemetry_rich=False)
    without_flag = onboard._build_map_prompt("sys-a", "c1", "f1")
    assert with_flag == without_flag, "default must stay byte-identical"
    assert "NEW CONCEPTS" not in without_flag


def test_map_prompt_guidance_has_no_demo_literals():
    denylist = ("Milwaukee", "Line1Filler", "AnyCompany", "WasherPump",
                "BottlingMachine", "Filler")
    for text in (onboard._TELEMETRY_RICH_NEW_CONCEPT_GUIDANCE,
                 onboard._build_map_prompt("s", "c", "f", telemetry_rich=True)):
        for word in denylist:
            assert word not in text, f"demo literal {word!r} leaked into the prompt"


# --------------------------------------------------------------------------- #
# Map job: sites (1) + (2)
# --------------------------------------------------------------------------- #
_LLM_MAPPINGS = [
    # Emergent (non-CORE) proposal — must survive for telemetry-rich systems.
    {"field": "tags.spindle_speed", "table": "tags",
     "mappings": [{"concept": "spindle-speed", "role": "measure",
                   "confidence": 0.9}]},
    # Unmapped field — defaults to sensor-reading only for telemetry-rich.
    {"field": "tags.mystery_channel", "table": "tags", "mappings": []},
]

_TABLES = [{"name": "tags",
            "columns": [{"name": "spindle_speed"}, {"name": "mystery_channel"}],
            "row_count": 2,
            "relation_kind": "table",
            "schema_revision": "rev-1"}]


class _FakeCatalog:
    def __init__(self, connection_type):
        self._ctype = connection_type

    def get(self, _sid):
        return {"id": _sid, "connection_type": self._ctype}


def _run_map_job(monkeypatch, connection_type):
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog(connection_type))
    monkeypatch.setattr(onboard, "get_all_concepts",
                        lambda: ["equipment-id", "sensor-reading"])
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: json.dumps(_LLM_MAPPINGS))
    monkeypatch.setattr(onboard, "_maybe_escalate_batch",
                        lambda bm, *_a, **_k: (bm, False, False))
    import agents.concept_suggest as concept_suggest
    monkeypatch.setattr(concept_suggest, "suggest_concepts_by_value_overlap",
                        lambda *_a, **_k: None)
    job_id = f"job-{connection_type.replace(' ', '-')}"
    onboard._run_map_job(job_id, "example-sys", [dict(t) for t in _TABLES])
    job = onboard._map_jobs.pop(job_id)
    assert job["status"] == "done", job
    return {m["field"]: m for m in job["result"]["mappings"]}


def test_i3x_map_job_keeps_emergent_concepts_and_defaults_sensor_reading(monkeypatch):
    by_field = _run_map_job(monkeypatch, "SiteWise (i3X)")
    # (1) the non-CORE proposal survived the map step
    spindle = by_field["tags.spindle_speed"]
    assert spindle["concept"] == "spindle-speed"
    assert spindle["mappings"] == [{"concept": "spindle-speed", "role": "measure",
                                    "confidence": 0.9}]
    # (2) the unmatched field defaulted to sensor-reading @0.7
    mystery = by_field["tags.mystery_channel"]
    assert mystery["concept"] == "sensor-reading"
    assert mystery["confidence"] == 0.7
    assert mystery["mappings"] == [{"concept": "sensor-reading", "role": "measure",
                                    "confidence": 0.7}]


def test_mcp_map_job_behavior_unchanged(monkeypatch):
    by_field = _run_map_job(monkeypatch, "MCP Server")
    assert by_field["tags.spindle_speed"]["concept"] == "spindle-speed"
    assert by_field["tags.mystery_channel"]["concept"] == "sensor-reading"


def test_postgres_map_job_still_filters_to_core_concepts(monkeypatch):
    by_field = _run_map_job(monkeypatch, "PostgreSQL")
    # Non-CORE proposal filtered → needs_review; unmatched stays needs_review.
    assert by_field["tags.spindle_speed"]["concept"] == "needs_review"
    assert by_field["tags.spindle_speed"]["mappings"] == []
    assert by_field["tags.mystery_channel"]["concept"] == "needs_review"


# --------------------------------------------------------------------------- #
# Telemetry-rich wedge: a concept-less LLM row must be DROPPED, never KeyError.
# The telemetry-rich path skips the vocabulary filter, so before the guard a
# row like {"role": "measure", "confidence": 0.8} (no 'concept') reached the
# r['concept'] subscript in _select_field_concepts, raised KeyError inside the
# map-job thread, and the outer except left the job at status="running"
# forever ("stops at mapping concepts" — observed live on a HighByte onboard).
# --------------------------------------------------------------------------- #
_CONCEPTLESS_ROW = {"role": "measure", "confidence": 0.8}


def test_select_field_concepts_drops_conceptless_rows_on_telemetry_rich():
    mappings = [{"field": "tags.pump_vibration", "table": "tags",
                 "mappings": [dict(_CONCEPTLESS_ROW),
                              {"concept": "vibration", "role": "measure",
                               "confidence": 0.9}]}]
    onboard._select_field_concepts(mappings, telemetry_rich=True,
                                   committable={"equipment-id"})
    m = mappings[0]
    # The concept-less row is gone; the valid emergent one survives.
    assert m["mappings"] == [{"concept": "vibration", "role": "measure",
                              "confidence": 0.9}]
    assert m["concepts"] == [{"concept": "vibration", "confidence": 0.9}]
    assert m["concept"] == "vibration"


def test_select_field_concepts_all_conceptless_rows_become_needs_review():
    mappings = [{"field": "tags.pump_vibration", "table": "tags",
                 "mappings": [dict(_CONCEPTLESS_ROW)]}]
    onboard._select_field_concepts(mappings, telemetry_rich=True,
                                   committable={"equipment-id"})
    assert mappings[0]["mappings"] == []
    assert mappings[0]["concept"] == "needs_review"


def test_telemetry_rich_map_job_survives_conceptless_llm_row(monkeypatch):
    """End-to-end through _run_map_job: the job reaches status='done', not
    a swallowed KeyError left at 'running'. The concept-less field falls
    through to the telemetry-rich sensor-reading default."""
    llm_rows = [
        {"field": "tags.spindle_speed", "table": "tags",
         "mappings": [{"concept": "spindle-speed", "role": "measure",
                       "confidence": 0.9}]},
        {"field": "tags.mystery_channel", "table": "tags",
         "mappings": [dict(_CONCEPTLESS_ROW)]},
    ]
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog("MCP Server"))
    monkeypatch.setattr(onboard, "get_all_concepts",
                        lambda: ["equipment-id", "sensor-reading"])
    monkeypatch.setattr(onboard, "bedrock_call",
                        lambda *_a, **_k: json.dumps(llm_rows))
    monkeypatch.setattr(onboard, "_maybe_escalate_batch",
                        lambda bm, *_a, **_k: (bm, False, False))
    import agents.concept_suggest as concept_suggest
    monkeypatch.setattr(concept_suggest, "suggest_concepts_by_value_overlap",
                        lambda *_a, **_k: None)
    job_id = "job-conceptless"
    onboard._run_map_job(job_id, "example-sys", [dict(t) for t in _TABLES])
    job = onboard._map_jobs.pop(job_id)
    assert job["status"] == "done", job
    by_field = {m["field"]: m for m in job["result"]["mappings"]}
    assert by_field["tags.spindle_speed"]["concept"] == "spindle-speed"
    assert by_field["tags.mystery_channel"]["concept"] == "sensor-reading"


# --------------------------------------------------------------------------- #
# Wide-table split: 20 columns per virtual chunk (not 50). At ~50 cols with
# 1-3 mappings per field the model's JSON array truncates mid-array and whole
# batches drop silently; 20 keeps every response under that threshold.
# --------------------------------------------------------------------------- #
def test_split_one_wide_table_chunks_at_twenty_columns():
    cols = [{"name": f"Site.Line1.Tag{i:03d}"} for i in range(101)]
    big = {"name": "NameSpace_MCP", "columns": cols, "row_count": 101}
    virtual_to_real: dict = {}
    out = onboard._split_one_wide_table("job-x", big, virtual_to_real)
    sizes = [len(t["columns"]) for t in out]
    assert sizes == [20, 20, 20, 20, 20, 1], sizes
    assert all(len(t["columns"]) <= 20 for t in out)
    # Every synthetic chunk maps back to the one real table.
    assert set(virtual_to_real.values()) == {"NameSpace_MCP"}
    assert len(virtual_to_real) == len(out)


# --------------------------------------------------------------------------- #
# Provision: site (3) — emergent concepts land as non-CORE Concept nodes
# --------------------------------------------------------------------------- #
class _FakeAgentManager:
    _agents: dict = {}

    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeProvisionCatalog:
    def __init__(self, entry=None):
        self._entry = entry

    def get(self, _sid):
        return self._entry

    def update_status(self, *_a, **_k):
        return None

    def get_onboarded_ids(self):
        return set()

    def get_onboarded(self):
        return []


def _install_provision_seams(monkeypatch, catalog_entry=None):
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, p or {})) or True)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub")
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr(onboard, "catalog", _FakeProvisionCatalog(catalog_entry))
    monkeypatch.setattr(onboard, "_classify_table_entity",
                        lambda *_a, **_k: {"entity": None, "mint": None,
                                           "identity_fields": []})
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system",
                        lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "accepted_user_concept_mappings",
                        lambda *_a, **_k: {})
    return writes


_EMERGENT_MAPPING = [
    {"table": "tags", "field": "tags.spindle_speed", "concept": "spindle-speed",
     "mappings": [{"concept": "spindle-speed", "role": "measure",
                   "confidence": 0.9}]},
]


def _concept_merge_ids(writes):
    return {p.get("cid") for c, p in writes
            if "MERGE (c:Concept {id: $cid})" in c}


def test_mcp_provision_lands_emergent_concept_node(monkeypatch):
    writes = _install_provision_seams(monkeypatch)
    body = ProvisionRequest(
        system_id="example-mcp", system_name="Example MCP", schema="",
        connection_type="MCP Server",
        connection_string="https://mcp.example.internal/mcp",
        mappings=list(_EMERGENT_MAPPING), rules=[],
    )
    onboard._do_provision(body)
    assert "spindle-speed" in _concept_merge_ids(writes), \
        "telemetry-rich provision must land emergent non-CORE concepts"
    maps_to = [(c, p) for c, p in writes
               if "MERGE (f)-[r:MAPS_TO]->(c)" in c and p.get("cid") == "spindle-speed"]
    assert maps_to, "the MAPS_TO edge for the emergent concept must be written"


def test_athena_provision_still_rejects_non_core_concepts(monkeypatch):
    from agents.connection_types import ATHENA
    writes = _install_provision_seams(monkeypatch)
    body = ProvisionRequest(
        system_id="example-athena", system_name="Example Athena",
        schema="example_db", connection_type=ATHENA,
        connection_config={"workgroup": "wg", "database": "example_db",
                           "region": "us-east-1"},
        mappings=list(_EMERGENT_MAPPING), rules=[],
    )
    onboard._do_provision(body)
    assert "spindle-speed" not in _concept_merge_ids(writes), \
        "non-telemetry systems keep the CORE_CONCEPTS filter"
