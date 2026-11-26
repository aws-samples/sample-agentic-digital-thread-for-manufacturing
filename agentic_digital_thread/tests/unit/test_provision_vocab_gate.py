# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision commit gate (contract C9) — M5 dotted-column survival end-to-end,
plus the M6 vocabulary-gate contract, through the REAL ``_do_provision``
schema-write.

LANDED (M5): ``_mapping_table_field`` preserves the verbatim table-prefixed
remainder, so a mapped multi-dot flattened column (``tbl`` +
``tbl.group_a.sub.code``) survives the Athena physical gate to a ``Field``
node id ``<sys>.tbl.group_a.sub.code`` WITH its MAPS_TO edge when
``group_a.sub.code`` is a REAL physical column; a phantom (non-physical)
column is still dropped.

LANDED (M6, T8): C9 also swaps the provision-side ``CORE_CONCEPTS`` membership
check for ``set(get_all_concepts())`` (the same non-retired vocabulary the map
prompt advertises), built ONCE per provision run in ``_make_provision_state``
and applied in ``_dp_commit_concept_edges`` (b2) and ``_dp_write_identity_rule``
(the LINKS_CONCEPT gate). The edit became landable after T8 decomposed
``_do_provision`` into ≤50-line phase helpers, satisfying the repo
maintainability gate (``test_response_integrity_iteration5/6``).

Every impure seam is recorded/stubbed (no AWS/Neptune/Postgres/LLM); fixtures
are generic — no dataset literals.
"""
from __future__ import annotations

import agents.onboard as onboard
from agents.catalog import CORE_CONCEPTS
from agents.connection_types import ATHENA
from agents.models import ProvisionRequest


class _FakeAgentManager:
    _agents: dict = {}

    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeSSM:
    def get_parameter(self, **_kwargs):
        raise RuntimeError("no SSM in unit tests")


def _install_seams(monkeypatch):
    """Record every ``neptune_write(cypher, params)`` and neutralize impure seams."""
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeSSM())
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    return writes


def _provision_athena(monkeypatch, mappings, physical=None):
    writes = _install_seams(monkeypatch)
    monkeypatch.setattr(
        onboard, "_discover_athena_physical_columns", lambda *_a, **_k: (physical or {})
    )
    body = ProvisionRequest(
        system_id="example-athena-x",
        system_name="Example Athena",
        schema="example_athena_db",
        connection_type=ATHENA,
        connection_config={"workgroup": "wg", "database": "example_athena_db", "region": "us-east-1"},
        mappings=mappings,
        rules=[],
    )
    onboard._do_provision(body)
    return writes


def _field_ids(writes):
    return [p["fid"] for c, p in writes if "MERGE (f:Field {id: $fid})" in c and "fid" in p]


def _maps_to(writes):
    return [
        (p.get("fid"), p.get("cid"))
        for c, p in writes
        if "MERGE (f)-[r:MAPS_TO]->(c)" in c
    ]


def _row(field, concept, table="wide_table", role="attribute", conf=0.8):
    return {"table": table, "field": field, "concept": concept,
            "mappings": [{"concept": concept, "role": role, "confidence": conf}]}


# --------------------------------------------------------------------------- #
# M5 (landed) — multi-dot survival + phantom drop through the physical gate
# --------------------------------------------------------------------------- #
def test_multi_dot_mapped_column_survives_and_phantom_still_dropped(monkeypatch):
    """C9/M5: ``wide_table.group_a.sub.code`` survives to Field id
    ``example-athena-x.wide_table.group_a.sub.code`` WITH its MAPS_TO edge when
    ``group_a.sub.code`` is physical; a phantom multi-dot column is still dropped
    (the #1 COLUMN_NOT_FOUND protection stays)."""
    physical = {
        "wide_table": [
            {"name": "id", "data_type": "varchar", "is_partition": False},
            {"name": "group_a.sub.code", "data_type": "integer", "is_partition": False},
        ]
    }
    writes = _provision_athena(
        monkeypatch,
        [
            _row("wide_table.group_a.sub.code", "batch"),
            _row("wide_table.group_a.sub.phantom", "batch"),  # NOT physical → dropped
        ],
        physical,
    )
    fids = set(_field_ids(writes))
    mapped = _maps_to(writes)
    assert "example-athena-x.wide_table.group_a.sub.code" in fids
    assert ("example-athena-x.wide_table.group_a.sub.code", "batch") in mapped
    assert "example-athena-x.wide_table.group_a.sub.phantom" not in fids
    assert all(fid != "example-athena-x.wide_table.group_a.sub.phantom"
               for fid, _cid in mapped)


def test_retired_and_hallucinated_concepts_rejected(monkeypatch):
    """A retired concept (absent from ``get_all_concepts``' C1 default) and a
    hallucinated id are rejected — no MAPS_TO edge, the Field itself is still
    written. Holds under BOTH the current CORE_CONCEPTS gate and the target M6
    vocabulary gate (neither id is core nor graph-known)."""
    monkeypatch.setattr(
        onboard, "get_all_concepts",
        lambda include_retired=False: sorted(set(CORE_CONCEPTS) | {"custom-quality-index"}),
    )
    writes = _provision_athena(
        monkeypatch,
        [
            _row("wide_table.old_metric", "legacy-retired-metric"),
            _row("wide_table.wild_guess", "made-up-concept"),
        ],
    )
    committed = {cid for _fid, cid in _maps_to(writes)}
    assert "legacy-retired-metric" not in committed
    assert "made-up-concept" not in committed
    # The columns themselves are never silently dropped by the concept gate.
    fids = set(_field_ids(writes))
    assert "example-athena-x.wide_table.old_metric" in fids
    assert "example-athena-x.wide_table.wild_guess" in fids


def test_core_concept_still_commits(monkeypatch):
    """A core concept keeps its MAPS_TO edge — unchanged before/after M6."""
    writes = _provision_athena(
        monkeypatch,
        [_row("wide_table.batch_no", "batch"),
         _row("wide_table.equipment_ref", "equipment-id")],
    )
    committed = {cid for _fid, cid in _maps_to(writes)}
    assert {"batch", "equipment-id"} <= committed


# --------------------------------------------------------------------------- #
# M6 (LANDED, T8) — the C9 committable-vocabulary gate
# --------------------------------------------------------------------------- #
def test_non_core_vocabulary_concept_commits(monkeypatch):
    """C9 target: a graph-known, non-retired concept OUTSIDE CORE_CONCEPTS
    writes its MAPS_TO edge (the CORE_CONCEPTS gate collapses every non-core
    pick today)."""
    assert "custom-quality-index" not in CORE_CONCEPTS
    monkeypatch.setattr(
        onboard, "get_all_concepts",
        lambda include_retired=False: sorted(set(CORE_CONCEPTS) | {"custom-quality-index"}),
    )
    writes = _provision_athena(
        monkeypatch,
        [_row("wide_table.quality_index", "custom-quality-index")],
    )
    assert ("example-athena-x.wide_table.quality_index",
            "custom-quality-index") in _maps_to(writes)


def test_committable_vocabulary_built_once_per_provision_run(monkeypatch):
    """C9 target: the committable vocabulary is computed exactly ONCE per
    provision run (no per-row graph reads)."""
    calls = {"n": 0}

    def counting(include_retired: bool = False):
        calls["n"] += 1
        return sorted(CORE_CONCEPTS)

    monkeypatch.setattr(onboard, "get_all_concepts", counting)
    _provision_athena(monkeypatch, [_row("wide_table.batch_no", "batch")])
    assert calls["n"] == 1
