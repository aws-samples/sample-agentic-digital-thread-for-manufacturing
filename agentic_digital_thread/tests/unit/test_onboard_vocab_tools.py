# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the two READ-ONLY onboarding vocab tools (ADR-0068).

``assess_vocabulary_fit`` relays ``vocab_fit.compute_fit`` verbatim (computed,
never model-judged) plus a server-owned context-question COUNT;
``propose_concepts`` is a zero-write preview riding the ONE shared import
classifier (``vocab_import_core``), so its actions cannot drift from what the
vocabulary import route would do on accept.

Fully offline: the session store, graph seams, and catalog reads are faked or
monkeypatched (the ``test_onboard_agent_gates.py`` idiom). Placeholder data
only (``system-a`` / ``widget`` style — neutrality ratchet).
"""
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import pytest  # noqa: E402

import agents.onboard_agent as oa  # noqa: E402
import agents.vocabulary_routes as vr  # noqa: E402
from agents.context_interview.questions import (  # noqa: E402
    schema_facts_from_tables,
    select_questions,
)
from agents.onboarding_session import SessionNotFoundError  # noqa: E402
from agents.vocab_fit import compute_fit  # noqa: E402

_TABLES = [
    {
        "name": "widget_orders",
        "columns": [
            {"name": "work_state", "data_type": "text"},
            {"name": "attempt_no", "data_type": "integer"},
            {"name": "created_at", "data_type": "timestamp"},
        ],
        "row_count": 5,
        "sample_values": {},
    },
]


class _FakeStore:
    """Minimal read-only stand-in for OnboardingSessionStore."""

    def __init__(self, sessions=None):
        self.sessions = sessions or {}

    def get_session(self, system_id):
        if system_id not in self.sessions:
            raise SessionNotFoundError(system_id)
        return dict(self.sessions[system_id])


@pytest.fixture
def graph_concepts(monkeypatch):
    """Pin the graph concept read to one extra non-core concept."""
    rows = [{"id": "work_state", "name": "Work state",
             "description": "Current processing stage of a widget."}]
    monkeypatch.setattr(oa, "neptune_query", lambda *a, **k: list(rows))
    return rows


def _install_session(monkeypatch, sessions):
    monkeypatch.setattr(oa, "_store", lambda: _FakeStore(sessions))


# --------------------------------------------------------------------------- #
# assess_vocabulary_fit
# --------------------------------------------------------------------------- #
def test_assess_returns_exact_compute_fit_dict_plus_count_fields(
        monkeypatch, graph_concepts):
    _install_session(monkeypatch, {
        "system-a": {"system_id": "system-a", "status": "discovered",
                     "discovered_tables": _TABLES}})
    # The ADR-0081 pending-bindings hint is store-backed; pin it offline
    # (its own filtering/degrade behavior is covered in
    # test_vocabulary_drift.py).
    monkeypatch.setattr(oa, "_pending_binding_count", lambda sid: 0)
    result = oa.assess_vocabulary_fit("system-a")

    expected_report = compute_fit(_TABLES, oa._vocabulary_concepts())
    expected_questions = len(
        select_questions(schema_facts_from_tables(_TABLES), set()))
    assert result == {"system_id": "system-a",
                      "context_questions": expected_questions,
                      "pending_bindings": 0,
                      **expected_report}
    # The graph concept must have contributed a candidate for work_state.
    assert result["columns_with_candidate"] >= 1
    # Determinism: same inputs twice => identical dict.
    assert oa.assess_vocabulary_fit("system-a") == result


def test_assess_errors_without_session(monkeypatch, graph_concepts):
    _install_session(monkeypatch, {})
    assert oa.assess_vocabulary_fit("system-a") == {
        "error": "No tables discovered yet. Call discover first."}


def test_assess_errors_without_discovered_tables(monkeypatch, graph_concepts):
    _install_session(monkeypatch, {
        "system-a": {"system_id": "system-a", "status": "discovering"}})
    assert "error" in oa.assess_vocabulary_fit("system-a")


def test_assess_works_when_reconsulted_after_map(monkeypatch, graph_concepts):
    # Unlike map_fields, the fit report must NOT require status=='discovered' —
    # re-consulting after mapping (or after concept accepts) is the point.
    _install_session(monkeypatch, {
        "system-a": {"system_id": "system-a", "status": "mapped",
                     "discovered_tables": _TABLES}})
    result = oa.assess_vocabulary_fit("system-a")
    assert "error" not in result
    assert result["system_id"] == "system-a"


def test_assess_graph_failure_degrades_to_core_vocabulary(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(oa, "neptune_query", _boom)
    _install_session(monkeypatch, {
        "system-a": {"system_id": "system-a", "status": "discovered",
                     "discovered_tables": _TABLES}})
    result = oa.assess_vocabulary_fit("system-a")
    assert "error" not in result
    assert result["vocabulary_size"] > 0  # curated core fallback


# --------------------------------------------------------------------------- #
# propose_concepts — zero-write preview
# --------------------------------------------------------------------------- #
def _no_write(monkeypatch):
    def _forbidden(*a, **k):
        raise AssertionError("propose_concepts must NEVER write to the graph")
    monkeypatch.setattr(oa, "neptune_write", _forbidden)


def test_propose_classifies_and_never_writes(monkeypatch):
    _no_write(monkeypatch)
    monkeypatch.setattr(
        oa, "neptune_query",
        lambda *a, **k: [{"id": "work_state"}, {"id": "widget-rate"}])
    result = oa.propose_concepts(
        "system-a",
        '[{"id": "work_state", "name": "Work state", "description": "d"},'
        ' {"id": "widget_rate", "name": "Widget rate", "description": "d"},'
        ' {"id": "attempt_no", "name": "Attempt number", "description": "d"}]')
    by_id = {p["id"]: p for p in result["proposals"]}
    assert by_id["work_state"]["action"] == "skip_exact"
    assert by_id["widget_rate"]["action"] == "skip_collision"
    assert by_id["widget_rate"]["collides_with"] == "widget-rate"
    assert by_id["attempt_no"]["action"] == "insert"
    assert by_id["attempt_no"]["description"] == "d"
    assert (result["insert_count"], result["skip_count"],
            result["collision_count"]) == (1, 1, 1)
    assert result["system_id"] == "system-a"


def test_propose_actions_match_the_admin_preview_route(monkeypatch):
    """Classification parity: the SAME rows through the tool and through
    ``vocabulary_routes._run_preview`` produce identical per-id actions."""
    _no_write(monkeypatch)
    existing = [{"id": "work_state"}, {"id": "widget-rate"}]
    monkeypatch.setattr(oa, "neptune_query", lambda *a, **k: list(existing))
    monkeypatch.setattr(vr, "_neptune_query", lambda *a, **k: list(existing))
    rows = [
        {"id": "work_state", "name": "Work state", "description": "d"},
        {"id": "widget_rate", "name": "Widget rate", "description": "d"},
        {"id": "attempt_no", "name": "Attempt number", "description": "d"},
        {"id": "attempt-no", "name": "Attempt number 2", "description": "d"},
    ]
    import json
    tool_result = oa.propose_concepts("system-a", json.dumps(rows))
    preview = vr._run_preview("batch-a", [dict(r) for r in rows])
    # Ordered per-row action parity (request order in both surfaces; the tool
    # slugifies ids the same way the route boundary does, so attempt-no
    # normalizes into an in-batch collision with attempt_no on BOTH paths).
    assert ([p["action"] for p in tool_result["proposals"]]
            == [r["action"] for r in preview["rows"]])
    assert (tool_result["insert_count"], tool_result["skip_count"],
            tool_result["collision_count"]) == (
        preview["insert_count"], preview["skip_count"],
        preview["collision_count"])


def test_propose_rejects_malformed_json(monkeypatch):
    _no_write(monkeypatch)
    assert "error" in oa.propose_concepts("system-a", "not json {")
    assert "error" in oa.propose_concepts("system-a", "{}")
    assert "error" in oa.propose_concepts("system-a", "[]")


def test_propose_bad_row_is_rejected_per_row_never_aborts(monkeypatch):
    _no_write(monkeypatch)
    monkeypatch.setattr(oa, "neptune_query", lambda *a, **k: [])
    result = oa.propose_concepts(
        "system-a",
        '[{"id": "", "name": "No id", "description": "d"},'
        ' {"id": "attempt_no", "name": "Attempt number", "description": "d"}]')
    assert "error" not in result
    actions = [p["action"] for p in result["proposals"]]
    assert "rejected" in actions and "insert" in actions
    assert result["rejected_count"] == 1
    rejected = [p for p in result["proposals"] if p["action"] == "rejected"]
    assert rejected[0]["reason"]


def test_propose_enforces_the_batch_cap(monkeypatch):
    _no_write(monkeypatch)
    import json
    rows = [{"id": f"col_{i}", "name": f"Col {i}", "description": "d"}
            for i in range(oa._PROPOSE_MAX_ROWS + 1)]
    assert "error" in oa.propose_concepts("system-a", json.dumps(rows))


def test_propose_graph_read_failure_degrades_to_insert(monkeypatch):
    _no_write(monkeypatch)
    def _boom(*a, **k):
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(oa, "neptune_query", _boom)
    result = oa.propose_concepts(
        "system-a", '[{"id": "attempt_no", "name": "Attempt", "description": "d"}]')
    assert result["proposals"][0]["action"] == "insert"


# --------------------------------------------------------------------------- #
# Wiring — ONBOARD_TOOLS carries both, explore tier, before the commit tools
# --------------------------------------------------------------------------- #
def test_both_tools_are_wired_into_onboard_tools():
    names = [t.tool_name for t in oa.ONBOARD_TOOLS]
    assert "assess_vocabulary_fit" in names
    assert "propose_concepts" in names
    assert len(names) == 13
    # Explore tier: both sit before the first commit-tier tool.
    assert names.index("assess_vocabulary_fit") < names.index("connect_new_system")
    assert names.index("propose_concepts") < names.index("connect_new_system")


def test_prompt_documents_the_interleave():
    assert "assess_vocabulary_fit" in oa.ONBOARD_AGENT_PROMPT
    assert "propose_concepts" in oa.ONBOARD_AGENT_PROMPT
    # The agent must never claim a concept was added (steward authority).
    assert "NEVER claim a concept was added" in oa.ONBOARD_AGENT_PROMPT
