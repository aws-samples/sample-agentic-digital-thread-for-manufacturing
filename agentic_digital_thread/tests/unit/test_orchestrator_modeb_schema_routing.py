# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Mode-B deterministic schema inject: the ``explore_graph`` wrapper
(``_explore_graph_with_schema``) UNCONDITIONALLY appends the compact schema
(connection_type + per-table columns/column_types/partition_keys/concept_map) of
EVERY onboarded system under ``routed_system_schemas`` — regardless of whether a
system id appears in the traversal result text — so the orchestrator composes
correct, bounded SQL on the first attempt (error parity with Mode A) without
electing to call ``get_system_schema``.

This replaces the earlier FRAGILE id/name textual match, which only fired when a
system id literally appeared in the traversal result string.

DRY: the wrapper reuses the canonical ``graph_tools.explore_graph`` traversal and the
cached ``schema_cache.get_or_load`` + ``schema_bundle.build_schema_bundle`` path; these
tests mock those seams and use only SYNTHETIC data (no dataset literals). Offline — no
AWS / LLM / gateway / Neptune.
"""
import json
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
import agents.schema_cache as schema_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    schema_cache.reset()
    orch._mcp_clients.clear()
    # Local-mode config so _get_orchestrator_tools never builds a gateway client.
    monkeypatch.setattr(
        orch, "get_config",
        lambda: SimpleNamespace(has_gateway=False, agentcore_gateway_url=None),
    )
    yield
    schema_cache.reset()


# --------------------------------------------------------------------------- #
# Synthetic seams (no dataset literals): a fake traversal result + system list +
# a mocked schema-bundle builder returning columns/types/partition_keys/concept_map.
# --------------------------------------------------------------------------- #
def _install_seams(monkeypatch, *, traversal_result, systems, build=None):
    """Patch the wrapper's reused seams.

    traversal_result: what the underlying _explore_graph returns (a JSON string).
    systems: list of {"id", "name", "connection_type"} rows returned by the
             MATCH (s:System) query.
    build: optional replacement for build_schema_bundle (default: synthetic bundle
           with columns, column_types, a partition_key, and a concept_map).
    """
    monkeypatch.setattr(orch, "_explore_graph", lambda cypher: traversal_result)
    monkeypatch.setattr("agents.db.neptune_query", lambda *_a, **_k: list(systems))
    monkeypatch.setattr(schema_cache, "cheap_schema_version", lambda _sid: "v1")

    def _default_build(system_id, include_samples=True):
        assert include_samples is False  # bounded: metadata only
        return {
            "system_id": system_id,
            "tables": {
                "t": {
                    "columns": ["c", "p"],
                    "column_types": {"c": "varchar", "p": "date"},
                    "partition_keys": ["p"],
                    "concept_map": {"c": {"concept": "concept.c"}},
                }
            },
            "rendered": "ignored-rendered-blob",
        }

    monkeypatch.setattr(
        "agents.schema_bundle.build_schema_bundle", build or _default_build
    )


def test_injects_schema_unconditionally_even_when_no_system_in_result(monkeypatch):
    """The inject fires UNCONDITIONALLY: schema is present for every onboarded system
    even when NO system id/name appears anywhere in the traversal result text."""
    traversal = json.dumps({"results": [{"note": "nothing routable here"}]})
    _install_seams(
        monkeypatch,
        traversal_result=traversal,
        systems=[
            {"id": "sys-alpha", "name": "Alpha", "connection_type": "Athena"},
            {"id": "sys-beta", "name": "Beta", "connection_type": "PostgreSQL"},
        ],
    )
    out = json.loads(orch._explore_graph_with_schema("MATCH (s:System) RETURN s"))
    # BOTH systems present although neither id/name appears in the result text.
    assert set(out["routed_system_schemas"]) == {"sys-alpha", "sys-beta"}
    # Original traversal payload preserved.
    assert out["results"] == [{"note": "nothing routable here"}]
    # A compose-SQL instruction note is attached.
    assert "routed_system_schemas_note" in out


def test_includes_connection_type_and_partition_keys(monkeypatch):
    """Each injected system carries its connection_type and the per-table schema
    includes columns, column_types, partition_keys, and concept_map."""
    traversal = json.dumps({"results": []})
    _install_seams(
        monkeypatch,
        traversal_result=traversal,
        systems=[{"id": "sys-a", "name": "Alpha", "connection_type": "Athena"}],
    )
    out = json.loads(orch._explore_graph_with_schema("q"))
    entry = out["routed_system_schemas"]["sys-a"]
    assert entry["connection_type"] == "Athena"
    tbl = entry["tables"]["t"]
    assert tbl["columns"] == ["c", "p"]
    assert tbl["partition_keys"] == ["p"]
    assert tbl["column_types"] == {"c": "varchar", "p": "date"}
    assert tbl["concept_map"] == {"c": {"concept": "concept.c"}}
    # Metadata-only: the rendered blob is NOT carried into the compact view.
    assert "rendered" not in entry
    assert "samples" not in tbl


def test_metadata_only_include_samples_false(monkeypatch):
    """The schema fetch is metadata-only (include_samples=False) — the default build
    asserts this, so a successful inject proves it was called correctly."""
    traversal = json.dumps({"results": []})
    _install_seams(
        monkeypatch,
        traversal_result=traversal,
        systems=[{"id": "sys-a", "name": "Alpha", "connection_type": "Athena"}],
    )
    out = json.loads(orch._explore_graph_with_schema("q"))
    assert "sys-a" in out["routed_system_schemas"]


def test_caps_appended_schemas(monkeypatch):
    """No more than _ROUTED_SCHEMA_CAP systems are appended (context bound)."""
    ids = [f"sys-{i}" for i in range(orch._ROUTED_SCHEMA_CAP + 3)]
    traversal = json.dumps({"results": []})
    _install_seams(
        monkeypatch,
        traversal_result=traversal,
        systems=[{"id": i, "name": i, "connection_type": "Athena"} for i in ids],
    )
    out = json.loads(orch._explore_graph_with_schema("q"))
    assert len(out["routed_system_schemas"]) == orch._ROUTED_SCHEMA_CAP


def test_dedupes_repeated_system_ids(monkeypatch):
    """A duplicate System row (same id) is injected once."""
    traversal = json.dumps({"results": []})
    _install_seams(
        monkeypatch,
        traversal_result=traversal,
        systems=[
            {"id": "sys-a", "name": "Alpha", "connection_type": "Athena"},
            {"id": "sys-a", "name": "Alpha", "connection_type": "Athena"},
        ],
    )
    out = json.loads(orch._explore_graph_with_schema("q"))
    assert set(out["routed_system_schemas"]) == {"sys-a"}


def test_best_effort_on_schema_fetch_failure(monkeypatch):
    """If schema fetch RAISES for one system, it is omitted and routing never fails;
    other systems are still injected."""
    traversal = json.dumps({"results": []})

    def _build(system_id, include_samples=True):
        if system_id == "sys-alpha":
            raise RuntimeError("schema fetch boom")
        return {"system_id": system_id, "tables": {}, "rendered": ""}

    _install_seams(
        monkeypatch,
        traversal_result=traversal,
        systems=[
            {"id": "sys-alpha", "name": "Alpha", "connection_type": "Athena"},
            {"id": "sys-beta", "name": "Beta", "connection_type": "PostgreSQL"},
        ],
        build=_build,
    )
    out = json.loads(orch._explore_graph_with_schema("q"))
    # sys-alpha failed and is omitted; sys-beta still appended.
    assert set(out["routed_system_schemas"]) == {"sys-beta"}


def test_all_schema_fetches_fail_returns_original(monkeypatch):
    """If EVERY system's schema fetch fails, return the original traversal unchanged."""
    traversal = json.dumps({"results": [{"id": "sys-alpha"}]})

    def _build(system_id, include_samples=True):
        raise RuntimeError("boom")

    _install_seams(
        monkeypatch,
        traversal_result=traversal,
        systems=[{"id": "sys-alpha", "name": "Alpha", "connection_type": "Athena"}],
        build=_build,
    )
    out = orch._explore_graph_with_schema("q")
    assert out == traversal
    assert "routed_system_schemas" not in json.loads(out)


def test_neptune_failure_returns_original(monkeypatch):
    """If the System-list query itself fails, routing returns the original result."""
    traversal = json.dumps({"results": [{"id": "sys-alpha"}]})
    monkeypatch.setattr(orch, "_explore_graph", lambda cypher: traversal)

    def _boom(*_a, **_k):
        raise RuntimeError("neptune down")

    monkeypatch.setattr("agents.db.neptune_query", _boom)
    out = orch._explore_graph_with_schema("q")
    assert out == traversal
    assert "routed_system_schemas" not in json.loads(out)


def test_no_onboarded_systems_returns_original(monkeypatch):
    """With zero onboarded systems, the original traversal result is returned."""
    traversal = json.dumps({"results": [{"note": "empty topology"}]})
    _install_seams(monkeypatch, traversal_result=traversal, systems=[])
    out = orch._explore_graph_with_schema("q")
    assert out == traversal
    assert "routed_system_schemas" not in json.loads(out)


def test_preserves_non_dict_traversal_payload(monkeypatch):
    """A non-dict traversal payload (e.g. a JSON list) is wrapped under 'results'
    and still enriched with routed_system_schemas."""
    traversal = json.dumps([{"row": 1}])
    _install_seams(
        monkeypatch,
        traversal_result=traversal,
        systems=[{"id": "sys-a", "name": "Alpha", "connection_type": "Athena"}],
    )
    out = json.loads(orch._explore_graph_with_schema("q"))
    assert out["results"] == [{"row": 1}]
    assert set(out["routed_system_schemas"]) == {"sys-a"}


# --------------------------------------------------------------------------- #
# Registration — the wrapper is the explore_graph tool in Mode B; plain in Mode A.
# --------------------------------------------------------------------------- #
def _tool_names(tools):
    return {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}


def test_mode_b_registers_wrapper_as_explore_graph(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    tools = orch._get_orchestrator_tools()
    # Same tool NAME 'explore_graph' is exposed (prompt/Quick unaffected)...
    assert "explore_graph" in _tool_names(tools)
    # ...and it is the schema-enriching variant, not the plain one.
    assert orch.explore_graph_with_schema in tools
    assert orch.explore_graph not in tools
    # get_system_schema stays registered in Mode B as a backup.
    assert "get_system_schema" in _tool_names(tools)


def test_mode_a_registers_plain_explore_graph(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    tools = orch._get_orchestrator_tools()
    assert orch.explore_graph in tools
    assert orch.explore_graph_with_schema not in tools
    assert "get_system_schema" not in _tool_names(tools)


def test_mode_unset_registers_plain_explore_graph(monkeypatch):
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    tools = orch._get_orchestrator_tools()
    assert orch.explore_graph in tools
    assert orch.explore_graph_with_schema not in tools


def test_mode_b_explore_graph_tool_name_matches_plain():
    """The Mode-B variant must expose the SAME tool name as the plain explore_graph
    so the prompt and AWS Quick MCP client are unaffected."""
    assert getattr(orch.explore_graph_with_schema, "tool_name", None) == getattr(
        orch.explore_graph, "tool_name", None
    ) == "explore_graph"


# --------------------------------------------------------------------------- #
# Prompt wording — Mode B references routed_system_schemas + the two error-parity
# rules (partition filter + non-SQL do-not-run_query); Mode A does NOT.
# --------------------------------------------------------------------------- #
def test_mode_b_prompt_references_routed_system_schemas():
    p = orch._build_orchestrator_prompt("orchestrator")
    assert "routed_system_schemas" in p
    # The backstop self-correction contract must remain.
    assert "schema_mismatch" in p and "schema_hint" in p
    # get_system_schema remains as a documented backup.
    assert "get_system_schema" in p


def test_mode_b_prompt_has_partition_filter_rule():
    """Mode B carries the one partition rule (bound the question's own time
    column; a partition column only when the question asks for its range;
    over-limit totals are computed exactly) and a LIMIT for row reads, never
    an instruction to always filter on partition_keys."""
    from agents.workflow_guidance import ATHENA_BOUND_RULE
    p = orch._build_orchestrator_prompt("orchestrator")
    assert ATHENA_BOUND_RULE in p
    assert "always add a where filter on the partition_keys" not in p.lower()


def test_mode_b_prompt_has_non_sql_do_not_run_query_rule():
    """Mode B must carry the SiteWise-QueryError-prevention rule: never run SQL against
    a non-SQL system (SiteWise / i3X / REST / MCP)."""
    p = orch._build_orchestrator_prompt("orchestrator")
    low = p.lower()
    assert "connection_type" in low, "Mode B must reference each system's connection_type"
    assert "do not call run_query with sql" in low, (
        "Mode B must forbid run_query SQL against a non-SQL system"
    )
    assert "will always fail" in low, (
        "Mode B must state run_query SQL against a non-SQL system always fails"
    )
    assert "never select *" in low, "Mode B must forbid SELECT *"


def test_mode_a_prompt_has_no_routed_system_schemas():
    p = orch._build_orchestrator_prompt("agent")
    assert "routed_system_schemas" not in p


def test_mode_a_prompt_absent_error_parity_rules():
    """The two Mode-B error-parity rules must be ABSENT from Mode A (byte-identical
    Mode A guarantee — Mode A delegates NL sub-questions and composes no SQL)."""
    p = orch._build_orchestrator_prompt("agent")
    low = p.lower()
    assert "partition_keys" not in low
    assert "do not call run_query with sql" not in low
    assert "never select *" not in low
