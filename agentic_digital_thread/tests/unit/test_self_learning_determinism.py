# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Self-learning determinism: the three levers that stop the Mode-B orchestrator
from repeatedly guessing nonexistent columns (COLUMN_NOT_FOUND) on wide SQL tables.

LEVER 1 (orchestrator): the Mode-B ``explore_graph`` schema inject foregrounds a
compact ``concept_to_column`` map (inverted ``concept_map``) per table and a
bind-first / never-guess note, so the model binds intent to EXACT columns.

LEVER 2 (runtime_app): a turn that hit COLUMN_NOT_FOUND persists the column
self-correction as DURABLE ``query_guidance`` IMMEDIATELY — no cross-turn
recurrence precondition.

LEVER 3 (runtime_app): the agent-analysis recommendation filter ACCEPTS a
concept->column binding (the useful anti-guessing hint) while still dropping
empty / infrastructure / 'ensure exists' noise.

Offline — no AWS / LLM / gateway / Neptune / network. Synthetic data only; no
dataset/system/model literals.
"""
import json
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
import agents.runtime_app as runtime_app
import agents.schema_cache as schema_cache


# ===========================================================================
# LEVER 1 — concept_to_column inversion + bind-first/never-guess note
# ===========================================================================
class TestInvertConceptMap:
    def test_role_absent_keys_on_concept(self):
        cm = {"col_a": {"concept": "concept.x"}}
        assert orch._invert_concept_map(cm) == {"concept.x": "col_a"}

    def test_role_present_keys_on_concept_slash_role(self):
        cm = {"col_a": {"concept": "concept.x", "role": "primary"}}
        assert orch._invert_concept_map(cm) == {"concept.x/primary": "col_a"}

    def test_columns_without_concept_are_skipped(self):
        # A note-only entry (disambiguation, no concept) binds nothing.
        cm = {"col_a": {"note": "just a note"}, "col_b": {"concept": "concept.y"}}
        assert orch._invert_concept_map(cm) == {"concept.y": "col_b"}

    def test_key_collision_first_column_wins(self):
        # Two columns claim the same concept[/role]; the first is kept (stable).
        cm = {"col_first": {"concept": "concept.z"}, "col_second": {"concept": "concept.z"}}
        assert orch._invert_concept_map(cm) == {"concept.z": "col_first"}

    def test_empty_and_malformed_are_tolerated(self):
        assert orch._invert_concept_map({}) == {}
        assert orch._invert_concept_map({"c": "not-a-dict"}) == {}


@pytest.fixture
def _clean_schema(monkeypatch):
    schema_cache.reset()
    orch._mcp_clients.clear()
    monkeypatch.setattr(
        orch, "get_config",
        lambda: SimpleNamespace(has_gateway=False, agentcore_gateway_url=None),
    )
    yield
    schema_cache.reset()


def test_explore_graph_emits_concept_to_column_and_note(monkeypatch, _clean_schema):
    """(a) The Mode-B inject emits an inverted concept_to_column per table and a
    note carrying the bind-first / never-guess instruction."""
    traversal = json.dumps({"results": []})
    monkeypatch.setattr(orch, "_explore_graph", lambda cypher: traversal)
    monkeypatch.setattr(
        "agents.db.neptune_query",
        lambda *_a, **_k: [{"id": "sys-a", "name": "Alpha", "connection_type": "Athena"}],
    )
    monkeypatch.setattr(schema_cache, "cheap_schema_version", lambda _sid: "v1")

    def _build(system_id, include_samples=True):
        assert include_samples is False  # metadata-only, bounded
        return {
            "system_id": system_id,
            "tables": {
                "t": {
                    "columns": ["c", "p", "d"],
                    "column_types": {"c": "varchar", "p": "date", "d": "date"},
                    "partition_keys": ["p"],
                    "concept_map": {
                        "c": {"concept": "concept.measure"},
                        "d": {"concept": "concept.time", "role": "event"},
                    },
                }
            },
            "rendered": "ignored",
        }

    monkeypatch.setattr("agents.schema_bundle.build_schema_bundle", _build)

    out = json.loads(orch._explore_graph_with_schema("q"))
    tbl = out["routed_system_schemas"]["sys-a"]["tables"]["t"]
    # concept_to_column is emitted and correctly inverted (concept, concept/role).
    assert tbl["concept_to_column"] == {
        "concept.measure": "c",
        "concept.time/event": "d",
    }
    # The raw concept_map is still carried alongside.
    assert tbl["concept_map"] == {
        "c": {"concept": "concept.measure"},
        "d": {"concept": "concept.time", "role": "event"},
    }
    # The note instructs bind-first + never-guess and references concept_to_column.
    note = out["routed_system_schemas_note"]
    assert "concept_to_column" in note
    assert "BIND" in note and "FIRST" in note
    assert "NEVER guess" in note


# ===========================================================================
# LEVER 2 — durable, immediate COLUMN_NOT_FOUND guidance (no recurrence gate)
# ===========================================================================
def _cnf_result(column: str) -> str:
    """A realistic Athena COLUMN_NOT_FOUND error text naming ``column``."""
    return (
        f"Error executing query: COLUMN_NOT_FOUND: line 1:8: Column '{column}' "
        f"cannot be resolved | available columns — T: real_a, real_b"
    )


def test_column_not_found_persists_one_durable_note(monkeypatch):
    """(b) A single turn with a COLUMN_NOT_FOUND for column 'X' on table 'T' triggers
    exactly ONE append_query_guidance_note write, naming 'X' and 'T', with NO
    existing-mapping / recurrence precondition."""
    calls = []
    monkeypatch.setattr(
        "agents.self_learning.append_query_guidance_note",
        lambda system_id, note, **kw: calls.append((system_id, note)) or "filed",
    )

    tool_calls_log = [{"name": "sys-a___run_query", "toolUseId": "u1"}]
    tool_inputs = {"u1": "{'sql': \"SELECT foo FROM db.T WHERE X = '2024-01-01'\"}"}
    tool_results = {"u1": _cnf_result("X")}

    written = runtime_app._persist_column_not_found_guidance(
        tool_calls_log, tool_inputs, tool_results
    )

    assert written == 1
    assert len(calls) == 1  # exactly one write, on the same turn, no recurrence gate
    system_id, note = calls[0]
    assert system_id == "sys-a"
    assert "'X'" in note  # the guessed column
    assert "T:" in note   # the failing table
    assert "get_system_schema" in note


def test_repeated_same_guess_in_turn_collapses_to_one_write(monkeypatch):
    """Two tool calls guessing the SAME (table, column) collapse to a single write."""
    calls = []
    monkeypatch.setattr(
        "agents.self_learning.append_query_guidance_note",
        lambda system_id, note, **kw: calls.append((system_id, note)) or "filed",
    )
    tool_calls_log = [
        {"name": "sys-a___run_query", "toolUseId": "u1"},
        {"name": "sys-a___run_query", "toolUseId": "u2"},
    ]
    tool_inputs = {
        "u1": "{'sql': 'SELECT foo FROM db.T WHERE X = 1'}",
        "u2": "{'sql': 'SELECT bar FROM db.T WHERE X = 2'}",
    }
    tool_results = {"u1": _cnf_result("X"), "u2": _cnf_result("X")}

    written = runtime_app._persist_column_not_found_guidance(
        tool_calls_log, tool_inputs, tool_results
    )
    assert written == 1
    assert len(calls) == 1


def test_no_write_without_column_not_found(monkeypatch):
    """A clean turn (no COLUMN_NOT_FOUND) writes nothing."""
    calls = []
    monkeypatch.setattr(
        "agents.self_learning.append_query_guidance_note",
        lambda *a, **k: calls.append(a) or "filed",
    )
    tool_calls_log = [{"name": "sys-a___run_query", "toolUseId": "u1"}]
    tool_inputs = {"u1": "{'sql': 'SELECT real_a FROM db.T LIMIT 10'}"}
    tool_results = {"u1": "rows: [{'real_a': 1}]"}
    assert runtime_app._persist_column_not_found_guidance(
        tool_calls_log, tool_inputs, tool_results
    ) == 0
    assert calls == []


def test_non_prefixed_tool_is_skipped(monkeypatch):
    """A COLUMN_NOT_FOUND on a non-system tool (no '<id>___' prefix) has no owning
    system and is skipped."""
    calls = []
    monkeypatch.setattr(
        "agents.self_learning.append_query_guidance_note",
        lambda *a, **k: calls.append(a) or "filed",
    )
    tool_calls_log = [{"name": "explore_graph", "toolUseId": "u1"}]
    tool_inputs = {"u1": "{'sql': 'SELECT foo FROM db.T WHERE X = 1'}"}
    tool_results = {"u1": _cnf_result("X")}
    assert runtime_app._persist_column_not_found_guidance(
        tool_calls_log, tool_inputs, tool_results
    ) == 0
    assert calls == []


def test_persist_is_best_effort_on_write_failure(monkeypatch):
    """A backend write failure is swallowed (best-effort) and never raises."""
    def _boom(*_a, **_k):
        raise RuntimeError("neptune down")

    monkeypatch.setattr("agents.self_learning.append_query_guidance_note", _boom)
    tool_calls_log = [{"name": "sys-a___run_query", "toolUseId": "u1"}]
    tool_inputs = {"u1": "{'sql': 'SELECT foo FROM db.T WHERE X = 1'}"}
    tool_results = {"u1": _cnf_result("X")}
    # Does not raise; nothing counted as written.
    assert runtime_app._persist_column_not_found_guidance(
        tool_calls_log, tool_inputs, tool_results
    ) == 0


# ===========================================================================
# LEVER 3 — relaxed filter keeps concept->column binding, drops noise
# ===========================================================================
def test_filter_keeps_concept_to_column_binding():
    """(c) A recommendation binding a concept/role to a specific real column is
    ACCEPTED — the useful anti-guessing hint."""
    rec = {
        "system": "sys-a",
        "table": "t",
        "field": "c",
        "annotation": "holds the concept.measure concept (primary role)",
    }
    assert runtime_app._recommendation_accepted(rec) is True


def test_filter_drops_empty_recommendation():
    assert runtime_app._recommendation_accepted(
        {"system": "sys-a", "table": "", "field": "", "annotation": ""}
    ) is False
    assert runtime_app._recommendation_accepted(
        {"system": "sys-a", "table": "N/A", "field": "N/A", "annotation": "x"}
    ) is False


def test_filter_drops_infrastructure_and_ensure_exists():
    assert runtime_app._recommendation_accepted(
        {"table": "t", "field": "c", "annotation": "fix the endpoint URL"}
    ) is False
    assert runtime_app._recommendation_accepted(
        {"table": "t", "field": "c", "annotation": "check the connection string"}
    ) is False
    assert runtime_app._recommendation_accepted(
        {"table": "t", "field": "c", "annotation": "ensure the column exists"}
    ) is False
