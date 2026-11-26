# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""X-3 containment — a try-it turn is a PURE READ.

``_is_tryit_session`` originally gated ONLY the usage-hook site; the other
post-turn capture writers (proposal re-minting via the discovery /
correlation / value-mapping hooks, ADR-0042 counter churn through
``log_proposed_mapping`` ingestion, query-guidance promotion from the
analysis persist) ran ungated on try-it replays. This file pins the fix:

* the FULL integrity post-turn path (``_integrity_post_turn_learning`` —
  the mirror of the legacy ``finally`` seam) with a ``tryit-`` session
  records ZERO writes at every DynamoDB-backed seam;
* the same path with a normal session still writes (non-tryit unchanged);
* each legacy capture writer honors the shared ``_skip_post_turn_capture``
  gate directly (covers the legacy ``finally`` call site — both sites call
  the SAME functions);
* the analysis-LLM persist (``_waste_analysis_events`` →
  ``_persist_analysis_recommendations``) is gated for try-it sessions.

Offline: no AWS, no network — every writer seam is a recorder.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import agents.artifact_usage as artifact_usage
from agents import runtime_app, self_learning

_CATALOG = [{"id": "erp", "name": "ERP"}, {"id": "cmms", "name": "CMMS"}]

_RESPONSE_WITH_PROPOSALS = (
    "Answer prose.\n"
    "---CORRELATION-PROPOSAL---\n"
    "Pattern: Machine faults in CMMS correlate with scrap spikes in ERP\n"
    "Confidence: HIGH\n"
    "---END-PROPOSAL---\n"
    "---VALUE-MAPPING-PROPOSAL---\n"
    "from_system: cmms\n"
    "from_field: assets.asset_ref\n"
    "from_value: A-77\n"
    "to_system: erp\n"
    "to_field: equipment.equipment_no\n"
    "to_value: 4400077\n"
    "concept: equipment-reference\n"
    "confidence: high\n"
    "method: value_format_match\n"
    "---END-PROPOSAL---\n"
)


class _RecordingTable:
    """DynamoDB fake — records every write, answers reads with nothing."""

    def __init__(self):
        self.writes: list = []

    def put_item(self, **kwargs):
        self.writes.append(("put_item", kwargs))
        return {}

    def update_item(self, **kwargs):
        self.writes.append(("update_item", kwargs))
        return {}

    def query(self, **_kwargs):
        return {"Items": []}

    def scan(self, **_kwargs):
        return {"Items": []}

    def get_item(self, **_kwargs):
        return {}


def _arm_write_recorders(monkeypatch):
    """Recorders on EVERY DynamoDB-backed writer seam + the table itself."""
    writes: list = []
    table = _RecordingTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(
        self_learning, "log_proposed_mapping",
        lambda **k: writes.append(("proposal", k.get("proposed_type")))
        or f"m-{len(writes)}")
    monkeypatch.setattr(
        self_learning, "record_rule_usage",
        lambda *a, **k: writes.append(("rule-bump", None)) or 1)
    monkeypatch.setattr(
        self_learning, "append_query_guidance_note",
        lambda *a, **k: writes.append(("guidance", None)) or True,
        raising=False)
    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage",
        lambda refs, evidence="": writes.append(("ledger", None)) or 1)
    monkeypatch.setattr(self_learning, "known_systems", lambda: _CATALOG)
    return writes, table


def _integrity_state():
    """A minimal integrity-invocation state for the mirror seam."""
    return SimpleNamespace(
        tool_names={"t1": "query_erp"},
        response_text=[_RESPONSE_WITH_PROPOSALS],
        seed_rule_index={},
        context=SimpleNamespace(tool_system_ids=None),
        rule_ids={"ir-x"},
        matched_rows=[{"neptune_node_id": "ir-x", "concept": "supplier"}],
    )


def _legacy_turn(session_id: str) -> SimpleNamespace:
    """The legacy finally-block ``s`` shape the three writers consume."""
    return SimpleNamespace(
        prompt="compare supplier ids", raw_prompt="compare supplier ids",
        session_id=session_id,
        tool_calls_log=[{"name": "query_erp", "toolUseId": "t1"}],
        tool_inputs={"t1": "{}"}, tool_results={"t1": "rows"},
        response_text=[_RESPONSE_WITH_PROPOSALS],
    )


# --------------------------------------------------------------------------- #
# Integrity mirror seam — the FULL post-turn path
# --------------------------------------------------------------------------- #
def test_tryit_session_full_post_turn_path_records_zero_writes(monkeypatch):
    writes, table = _arm_write_recorders(monkeypatch)

    runtime_app._integrity_post_turn_learning(
        _integrity_state(), "compare supplier ids", "tryit-job1-a")

    assert writes == []        # no proposals, no counters, no ledger
    assert table.writes == []  # the DynamoDB fake saw NOTHING


def test_non_tryit_session_full_post_turn_path_still_writes(monkeypatch):
    writes, _table = _arm_write_recorders(monkeypatch)

    runtime_app._integrity_post_turn_learning(
        _integrity_state(), "compare supplier ids", "sess-1")

    kinds = {w[0] for w in writes}
    assert "rule-bump" in kinds and "ledger" in kinds
    proposal_types = {w[1] for w in writes if w[0] == "proposal"}
    assert {"insight", "value_mapping"} <= proposal_types


# --------------------------------------------------------------------------- #
# Legacy finally-block writers — the SAME functions, gated at their tops
# --------------------------------------------------------------------------- #
def test_each_capture_writer_skips_tryit_sessions(monkeypatch):
    writes, table = _arm_write_recorders(monkeypatch)
    s = _legacy_turn("tryit-job1-b")

    runtime_app._post_turn_learning_checks(s)
    runtime_app._capture_correlation_insights(s)
    runtime_app._capture_value_mapping_proposals(s)

    assert writes == []
    assert table.writes == []


def test_capture_writers_still_fire_for_normal_sessions(monkeypatch):
    writes, _table = _arm_write_recorders(monkeypatch)
    s = _legacy_turn("chat-session-1")

    runtime_app._capture_correlation_insights(s)
    runtime_app._capture_value_mapping_proposals(s)

    proposal_types = {w[1] for w in writes if w[0] == "proposal"}
    assert {"insight", "value_mapping"} <= proposal_types


def test_capture_writers_consult_the_shared_gate():
    """Wiring tripwire: every capture writer goes through the ONE predicate
    (so the legacy finally block and the integrity mirror stay covered)."""
    for fn in (runtime_app._post_turn_learning_checks,
               runtime_app._capture_correlation_insights,
               runtime_app._capture_value_mapping_proposals):
        assert "_skip_post_turn_capture" in inspect.getsource(fn), fn.__name__


def test_skip_predicate_covers_tryit_and_briefing():
    assert runtime_app._skip_post_turn_capture(_legacy_turn("tryit-x-a"))
    briefing = _legacy_turn("sess-1")
    briefing.prompt = "Generate the operations briefing"
    assert runtime_app._skip_post_turn_capture(briefing)
    assert not runtime_app._skip_post_turn_capture(_legacy_turn("sess-1"))


# --------------------------------------------------------------------------- #
# Analysis persist — proposals/guidance from the waste analysis are gated
# --------------------------------------------------------------------------- #
def _drive_waste_analysis(monkeypatch, session_id: str) -> list:
    persisted: list = []
    monkeypatch.setattr(
        runtime_app, "_analysis_call_log", lambda s: (["line"], 1))
    monkeypatch.setattr(
        runtime_app, "_analysis_prompt_text", lambda *a: "prompt")
    monkeypatch.setattr(
        runtime_app, "_invoke_analysis_model", lambda _p: "{}")
    monkeypatch.setattr(
        runtime_app, "_parsed_analysis",
        lambda *_a: ({"wasted_calls": [{"step": 1}], "deterministic_pct": 50},
                     [{"system": "sys-a", "table": "t", "field": "f",
                       "annotation": "note"}]))
    monkeypatch.setattr(
        runtime_app, "_persist_analysis_recommendations",
        lambda *a, **k: persisted.append(a))
    s = _legacy_turn(session_id)
    list(runtime_app._waste_analysis_events(s, {"wasted_steps": [1]}, 3, 0))
    return persisted


def test_waste_analysis_never_persists_for_tryit(monkeypatch):
    assert _drive_waste_analysis(monkeypatch, "tryit-job1-a") == []


def test_waste_analysis_still_persists_for_normal_sessions(monkeypatch):
    assert len(_drive_waste_analysis(monkeypatch, "sess-1")) == 1
