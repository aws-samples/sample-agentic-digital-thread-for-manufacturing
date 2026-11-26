# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 2 (Task 2.1) — Mode A bakes capped/redacted samples into ``tables_desc`` and
writes a ``schema_version`` on the System node, WITHOUT ever writing sample values to
the graph (rules-engine constraint).

Exercises the REAL ``_do_provision`` schema-write with every impure seam
recorded/stubbed (no AWS/Neptune/Postgres/LLM). Synthetic data only — no dataset
literals.
"""
from __future__ import annotations

import agents.onboard as onboard
import agents.resolve_evidence as resolve_evidence
from agents.models import ProvisionRequest

# Module-level monotonic clock so successive provisions get distinct schema_version
# stamps (each provision reads it at least once via str(int(time.time()*1000))).
_CLOCK = {"t": 1_700_000_000.0}


def _fake_time():
    _CLOCK["t"] += 1.0
    return _CLOCK["t"]


class _RecordingAgentManager:
    _agents: dict = {}

    def __init__(self):
        self.start_kwargs: dict = {}

    def start_agent(self, **kwargs):
        self.start_kwargs = kwargs
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


def _install_seams(monkeypatch, agent_manager, samples_by_field=None):
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub prompt")
    # information_schema.columns lookup returns REAL (clean) column names + types.
    monkeypatch.setattr(
        onboard, "pg",
        lambda *_a, **_k: {"rows": [("widget_code", "varchar"), ("email", "varchar")]},
    )
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", agent_manager)
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: object())
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])

    # Synthetic per-field sampling — keyed by field name so PII vs non-PII is testable.
    samples_by_field = samples_by_field or {}

    def _fake_sample(*, field="", **_k):
        return samples_by_field.get(field, [])

    monkeypatch.setattr(resolve_evidence, "sample_field_values", _fake_sample)
    monkeypatch.setattr(onboard.time, "time", _fake_time)
    return writes


def _provision_pg(monkeypatch, mappings, samples_by_field=None):
    am = _RecordingAgentManager()
    writes = _install_seams(monkeypatch, am, samples_by_field)
    body = ProvisionRequest(
        system_id="widgets-x",
        system_name="Widgets X",
        schema="widget_schema",
        connection_type="PostgreSQL",
        connection_config={},
        mappings=mappings,
        rules=[],
    )
    onboard._do_provision(body)
    return writes, am


_MAPPINGS = [
    {"table": "widgets", "field": "widgets.widget_code", "concept": "equipment-id",
     "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9}]},
    {"table": "widgets", "field": "widgets.email", "concept": "needs_review",
     "mappings": []},
]


# --------------------------------------------------------------------------- #
# Sample baking into tables_desc
# --------------------------------------------------------------------------- #
def test_tables_desc_bakes_samples_for_non_pii_and_redacts_pii(monkeypatch):
    writes, am = _provision_pg(
        monkeypatch, _MAPPINGS,
        samples_by_field={"widget_code": ["W-1", "W-2", "W-1"], "email": ["user1@example.com"]},
    )
    tables_desc = am.start_kwargs.get("tables_desc", "")
    # Non-PII column: an example-value fragment is baked in (deduped).
    assert "e.g. W-1, W-2" in tables_desc
    # PII-named column: NO raw value, [redacted] instead.
    assert "email: varchar [redacted]" in tables_desc
    assert "user1@example.com" not in tables_desc


def test_sample_enrichment_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    writes, am = _provision_pg(
        monkeypatch, _MAPPINGS,
        samples_by_field={"widget_code": ["W-1", "W-2"]},
    )
    tables_desc = am.start_kwargs.get("tables_desc", "")
    # No sampling → columns-only render, no example values.
    assert "e.g." not in tables_desc
    assert "widget_code" in tables_desc


# --------------------------------------------------------------------------- #
# Rules-engine constraint: NO sample values ever written to the graph
# --------------------------------------------------------------------------- #
def test_no_sample_payload_written_to_graph(monkeypatch):
    writes, _am = _provision_pg(
        monkeypatch, _MAPPINGS,
        samples_by_field={"widget_code": ["W-1", "W-2", "W-3"], "email": ["user1@example.com"]},
    )
    for cypher, params in writes:
        low = cypher.lower()
        # No write statement mentions samples/example values.
        assert "sample" not in low
        assert "example" not in low
        # No param key carries a sample payload.
        for key in params:
            assert "sample" not in key.lower()
        # The concrete sampled values never appear in ANY graph write param.
        flat = " ".join(str(v) for v in params.values())
        assert "W-1" not in flat
        assert "user1@example.com" not in flat


# --------------------------------------------------------------------------- #
# schema_version stamp on the System node
# --------------------------------------------------------------------------- #
def _schema_version_writes(writes):
    return [p for c, p in writes if "s.schema_version = $ver" in c and "ver" in p]


def test_provision_writes_schema_version(monkeypatch):
    writes, _am = _provision_pg(monkeypatch, _MAPPINGS)
    vers = _schema_version_writes(writes)
    assert len(vers) == 1
    assert vers[0]["ver"] and isinstance(vers[0]["ver"], str)
    assert vers[0]["sid"] == "widgets-x"


def test_second_provision_bumps_schema_version(monkeypatch):
    w1, _ = _provision_pg(monkeypatch, _MAPPINGS)
    v1 = _schema_version_writes(w1)[0]["ver"]
    w2, _ = _provision_pg(monkeypatch, _MAPPINGS)
    v2 = _schema_version_writes(w2)[0]["ver"]
    assert v1 != v2
