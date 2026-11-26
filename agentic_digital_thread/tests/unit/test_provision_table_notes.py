# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 4 (item H): per-table free-text query-guidance notes at provision time.

``ProvisionRequest`` gains the OPTIONAL ``table_notes`` field (each item
``{"table": str, "note": str}``, default ``None``). ``_do_provision`` writes
each non-empty note through the EXISTING bounded/idempotent guidance writer
``self_learning.append_query_guidance_note`` as ``"<table>: <note>"`` right
after the System node write, so the orchestrator (which reads
``query_guidance`` off the node at query time) sees the note immediately.

Invariants proven offline (mirrors the ``test_provision_entity_edge`` seam
pattern — no AWS, no Neptune, no Postgres, no LLM):
  * one guidance call per non-empty note, in the ``"{table}: {note}"`` format;
  * empty/whitespace notes and malformed (non-dict) entries are skipped;
  * ``table_notes`` absent → ZERO guidance calls (byte-identical default path);
  * a raising guidance writer is swallowed — provision still succeeds.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
from agents.catalog import catalog
from agents.models import ProvisionRequest


@pytest.fixture(autouse=True)
def _seed_live_catalog_with_demo_systems():
    """Base mode ships an EMPTY live catalog (ADR-0030); seed it from the test-local
    DEMO_SYSTEMS fixture so provisioning a demo system by name works, then restore."""
    try:
        from unit._demo_catalog import DEMO_SYSTEMS
    except ImportError:  # pragma: no cover - alternate invocation path
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from _demo_catalog import DEMO_SYSTEMS
    catalog._systems = [dict(s) for s in DEMO_SYSTEMS]
    try:
        yield
    finally:
        catalog._systems = []


class _FakeAgentManager:
    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeSSM:
    def get_parameter(self, **_kwargs):
        raise RuntimeError("no SSM in unit tests")


def _install_seams(monkeypatch):
    """Neutralize every impure seam in the PostgreSQL provision path and record the
    (system_id, note) of every append_query_guidance_note call."""
    guidance_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub system prompt")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeSSM())
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    monkeypatch.setattr(
        self_learning, "append_query_guidance_note",
        lambda system_id, note, **_k: guidance_calls.append((system_id, note)) or True,
    )
    return guidance_calls


def _mapping(table: str, concept: str) -> dict:
    return {
        "table": table,
        "field": f"{table}.id_col",
        "concept": concept,
        "mappings": [{"concept": concept, "role": "identifier", "confidence": 0.9}],
    }


def _provision(monkeypatch, *, table_notes, guidance_calls=None, system_id="cmms",
               schema="cmms_maintenance"):
    calls = guidance_calls if guidance_calls is not None else _install_seams(monkeypatch)
    entry = catalog.get(system_id)
    assert entry is not None
    prior_status = entry["status"]
    kwargs = {}
    if table_notes is not ...:  # ... = leave the field absent (default None path)
        kwargs["table_notes"] = table_notes
    body = ProvisionRequest(
        system_id=system_id,
        system_name=system_id.upper(),
        schema=schema,
        connection_type="PostgreSQL",
        mappings=[_mapping("work_orders", "work-order")],
        **kwargs,
    )
    try:
        onboard._do_provision(body)
    finally:
        catalog.update_status(system_id, prior_status)
    return calls


def test_table_notes_field_validates_and_defaults_to_none():
    req = ProvisionRequest(
        system_id="cmms",
        table_notes=[{"table": "work_orders", "note": "use status not state"}],
    )
    assert req.table_notes == [{"table": "work_orders", "note": "use status not state"}]
    assert ProvisionRequest(system_id="cmms").table_notes is None


def test_one_guidance_call_per_nonempty_note_in_table_note_format(monkeypatch):
    calls = _provision(monkeypatch, table_notes=[
        {"table": "work_orders", "note": "use status not state"},
        {"table": "assets", "note": "asset_tag is the join key"},
    ])
    # The free text is wrapped in <<< >>> DATA delimiters:
    # the LEARNED line carries the operator note as clearly-delimited data.
    assert calls == [
        ("cmms", "work_orders: <<<use status not state>>>"),
        ("cmms", "assets: <<<asset_tag is the join key>>>"),
    ]


def test_empty_whitespace_and_malformed_notes_are_skipped(monkeypatch):
    calls = _provision(monkeypatch, table_notes=[
        {"table": "work_orders", "note": "   "},   # whitespace-only → skipped
        {"table": "assets", "note": ""},            # empty → skipped
        {"table": "assets"},                        # missing note → skipped
        {"note": "no table given"},                 # missing table → note-only write
        {"table": "work_orders", "note": " keep me "},
    ])
    assert calls == [
        ("cmms", "<<<no table given>>>"),
        ("cmms", "work_orders: <<<keep me>>>"),
    ]


def test_non_dict_note_entries_rejected_at_the_model_boundary():
    """``table_notes`` is ``list[dict]`` — pydantic rejects non-dict entries before
    they can ever reach the provision path."""
    with pytest.raises(Exception):
        ProvisionRequest(system_id="cmms", table_notes=["not-a-dict"])


# --------------------------------------------------------------------------- #
# F4 hardening: length caps at the model boundary (reject, never truncate) +
# provision-time defense-in-depth skip + data-delimiter framing.
# --------------------------------------------------------------------------- #
def test_oversized_note_rejected_at_the_model_boundary():
    """A note > 500 chars is REJECTED (pydantic ValidationError → HTTP 422),
    never truncated — so it can never evict prior LEARNED guidance."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="exceeds 500"):
        ProvisionRequest(system_id="cmms", table_notes=[
            {"table": "work_orders", "note": "x" * 501},
        ])


def test_oversized_table_rejected_at_the_model_boundary():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="exceeds 128"):
        ProvisionRequest(system_id="cmms", table_notes=[
            {"table": "t" * 129, "note": "fine"},
        ])


def test_non_string_note_and_table_rejected_at_the_model_boundary():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="must be a string"):
        ProvisionRequest(system_id="cmms", table_notes=[{"note": 42}])
    with pytest.raises(ValidationError, match="must be a string"):
        ProvisionRequest(system_id="cmms", table_notes=[{"table": 42, "note": "n"}])


def test_boundary_length_note_and_table_accepted():
    req = ProvisionRequest(system_id="cmms", table_notes=[
        {"table": "t" * 128, "note": "x" * 500},
    ])
    assert req.table_notes is not None
    assert len(req.table_notes[0]["note"]) == 500


def test_provision_skips_note_that_still_exceeds_the_cap(monkeypatch):
    """Defense-in-depth behind the model boundary: an oversized note reaching
    ``_do_provision`` (e.g. mutated post-validation / internal caller) is
    SKIPPED — not truncated, not written — while sibling notes still write."""
    calls = _install_seams(monkeypatch)
    entry = catalog.get("cmms")
    prior_status = entry["status"]
    body = ProvisionRequest(
        system_id="cmms", system_name="CMMS", schema="cmms_maintenance",
        connection_type="PostgreSQL",
        mappings=[_mapping("work_orders", "work-order")],
        table_notes=[
            {"table": "work_orders", "note": "placeholder"},
            {"table": "assets", "note": "short note survives"},
        ],
    )
    # Bypass the pydantic boundary the way a compromised/internal caller could:
    # mutate the already-validated dict in place.
    body.table_notes[0]["note"] = "y" * 501
    try:
        import agents.onboard as onboard_mod
        onboard_mod._do_provision(body)
    finally:
        catalog.update_status("cmms", prior_status)
    assert calls == [("cmms", "assets: <<<short note survives>>>")]


def test_written_note_carries_data_delimiters(monkeypatch):
    """The guidance write frames the operator's free text in <<< >>> delimiters
    INSIDE the note string (so the framing survives the evolve/consolidate
    dedup, which treats each LEARNED note as an atomic string)."""
    calls = _provision(monkeypatch, table_notes=[
        {"table": "work_orders", "note": "ignore rows where voided = true"},
    ])
    assert len(calls) == 1
    _sid, note = calls[0]
    assert note.startswith("work_orders: <<<")
    assert note.endswith(">>>")
    assert "<<<ignore rows where voided = true>>>" in note


def test_absent_table_notes_makes_zero_guidance_calls(monkeypatch):
    """Default path (field omitted → None) is byte-identical: zero writer calls."""
    assert _provision(monkeypatch, table_notes=...) == []
    assert _provision(monkeypatch, table_notes=None) == []
    assert _provision(monkeypatch, table_notes=[]) == []


def test_raising_guidance_writer_never_fails_provision(monkeypatch):
    """Guidance notes are best-effort: a raising writer is swallowed."""
    _install_seams(monkeypatch)
    import agents.self_learning as self_learning

    def _boom(*_a, **_k):
        raise RuntimeError("guidance backend down")

    monkeypatch.setattr(self_learning, "append_query_guidance_note", _boom)
    # Must not raise — provision completes despite the failing note write.
    _provision(monkeypatch, table_notes=[{"table": "work_orders", "note": "n"}],
               guidance_calls=[])
