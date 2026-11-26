# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Single-source gate for the intelligence-summary payload (rules-applied plan).

``agents/query_intelligence.py`` is the ONE home for the graph-query
predicate, the FAILED/empty-result envelope markers, and the
intelligence-summary payload shape (``build_intelligence_summary`` —
Decision 2 of the rules-applied plan). This gate fails if ``runtime_app``
re-grows a private copy of any of them, or resurrects the deleted
``graph_guided_steps`` payload key (deleted, not aliased).

``deterministic_pct`` RETURNED — but only as the MECHANICAL
determinism score (``agents/determinism.py``), never the old LLM-judged
number: runtime_app must delegate the computation to ``score_tool_calls``,
feed the ONE builder via the ``determinism=`` kwarg, never ask the analysis
LLM for the number, and override whatever the LLM emits.

Source-inspection idiom mirrors ``test_demo_schema_routing_single_source.py``
/ ``test_agent_analysis_panel.py``. Offline: no AWS, no network.
"""
from __future__ import annotations

from pathlib import Path

_RUNTIME_APP = Path(__file__).resolve().parents[2] / "agents" / "runtime_app.py"
_SRC = _RUNTIME_APP.read_text(encoding="utf-8")


def test_summary_payload_built_by_the_single_source():
    # The payload comes from the ONE builder — never an inline dict.
    assert "query_intelligence.build_intelligence_summary(" in _SRC
    assert '"intelligence_summary": {' not in _SRC, \
        "inline intelligence_summary dict resurrected — use build_intelligence_summary"


def test_deleted_payload_keys_stay_deleted():
    # graph_guided_steps is gone everywhere (payload-only key, no other surface).
    assert "graph_guided_steps" not in _SRC


def test_determinism_score_is_computed_never_llm_or_inline():
    """deterministic_pct is the MECHANICAL score, single-sourced."""
    # runtime_app delegates the computation to agents/determinism.py …
    assert "from .determinism import score_tool_calls" in _SRC
    # … feeds the ONE builder through the additive kwarg (never an inline
    # summary-key assignment) …
    assert "determinism=_det_score" in _SRC
    assert '_summary_payload["deterministic_pct"]' not in _SRC
    assert '_summary_payload["grounded_steps"]' not in _SRC
    # … never asks the analysis LLM for the number (the old prompt item) …
    assert "Integer 0-100" not in _SRC
    # … and overrides whatever the LLM emitted with the computed score.
    assert ('analysis_data["deterministic_pct"] = '
            '_det_score["deterministic_pct"]') in _SRC


def test_graph_predicate_delegates_to_query_intelligence():
    # _is_explore_graph keeps its name (test_neptune_queries_counter.py
    # imports it) but its body is the shared predicate.
    assert "query_intelligence.is_graph_query(" in _SRC
    # The inline predicate expression must not re-grow a private copy.
    assert 'name == "explore_graph" or name.endswith' not in _SRC


def test_failure_markers_live_only_in_query_intelligence():
    # runtime_app no longer defines the envelope-marker tuples; both the
    # summary's wasted count and the agent-analysis FAILED tagging go through
    # the shared helpers so the definitions cannot drift.
    assert "_EMPTY_MARKERS" not in _SRC
    assert "_ERROR_MARKERS" not in _SRC
    assert "query_intelligence.is_failed_result(" in _SRC
    assert "query_intelligence.count_wasted_calls(" in _SRC
