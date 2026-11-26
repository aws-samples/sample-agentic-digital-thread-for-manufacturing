# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision REPLACES (not appends to) the system's schema subgraph.

LIVE-CONFIRMED regression: re-onboarding a system left STALE SchemaEntry/Field/
MAPS_TO nodes from the previous onboarding in the graph (provision wrote the
fresh schema subgraph ADDITIVELY), so columns/properties dropped from the source
kept being selected by the read paths (entity_render.read_sections,
metric_resolver) → runtime "column/property not found" errors.

These tests exercise the REAL ``_do_provision`` (every impure seam stubbed — no
AWS/Neptune/Postgres/LLM) and prove:

(a) provision issues the scoped delete BEFORE any schema write, exactly once,
    only for the target system id;
(b) the delete cypher touches ONLY the system's SchemaEntry/Field subgraph
    (query string + params asserted);
(c) a delete failure ABORTS the provision with a clear error — no partial
    (mixed stale+fresh) schema write;
(d) Concepts / Assets / JoinPaths / IdentityRules / ValueMappings /
    AuthorityRules never appear in the delete statement.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
from agents.connection_types import ATHENA
from agents.models import ProvisionRequest


# --------------------------------------------------------------------------- #
# Harness (mirrors test_provision_athena_schema_write, plus ordered call
# recording across BOTH neptune seams and per-cypher write return control).
# --------------------------------------------------------------------------- #
class _FakeAgentManager:
    _agents: dict = {}

    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeSSM:
    def get_parameter(self, **_kwargs):
        raise RuntimeError("no SSM in unit tests")


def _install_seams(monkeypatch, *, write_returns=None, query_returns=None):
    """Record every neptune call in ORDER; neutralize all other impure seams.

    ``write_returns``: {cypher_substring: return_value} — first matching
    substring wins; unmatched writes return True (the real success value).
    ``query_returns``: {cypher_substring: rows} — unmatched queries return [].
    """
    calls: list[tuple[str, str, dict]] = []  # (kind, cypher, params)

    def _nw(cypher, params=None):
        calls.append(("write", cypher, params or {}))
        for pat, val in (write_returns or {}).items():
            if pat in cypher:
                return val
        return True

    def _nq(cypher, params=None):
        calls.append(("query", cypher, params or {}))
        for pat, rows in (query_returns or {}).items():
            if pat in cypher:
                return rows
        return []

    monkeypatch.setattr(onboard, "neptune_write", _nw)
    monkeypatch.setattr(onboard, "neptune_query", _nq)
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeSSM())
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    return calls


_MAPPINGS = [
    {"column_name": "serial_number", "concept": "equipment-id",
     "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9}]},
    {"column_name": "work_order_id", "concept": "work-order",
     "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.9}]},
]


def _body(system_id="example-athena-x"):
    return ProvisionRequest(
        system_id=system_id,
        system_name="Example Athena",
        schema="example_athena_db",
        connection_type=ATHENA,
        connection_config={"workgroup": "wg", "database": "example_athena_db",
                           "region": "us-east-1"},
        mappings=_MAPPINGS,
        rules=[],
    )


def _delete_writes(calls):
    return [(c, p) for kind, c, p in calls
            if kind == "write" and "DETACH DELETE se, f" in c]


def _schema_write_indices(calls):
    """Indices of every fresh schema write (SchemaEntry/Field MERGE + HAS_FIELD)."""
    return [i for i, (kind, c, _p) in enumerate(calls)
            if kind == "write" and (
                "MERGE (se:SchemaEntry" in c
                or "MERGE (f:Field" in c
                or "MERGE (se)-[:HAS_FIELD]->(f)" in c)]


# --------------------------------------------------------------------------- #
# (a) delete BEFORE schema writes, exactly once, for the target system only
# --------------------------------------------------------------------------- #
def test_provision_deletes_stale_subgraph_once_before_schema_writes(monkeypatch):
    calls = _install_seams(monkeypatch)
    onboard._do_provision(_body())

    deletes = _delete_writes(calls)
    assert len(deletes) == 1, "the scoped schema delete must run EXACTLY once"
    _cypher, params = deletes[0]
    assert params == {"sid": "example-athena-x"}, \
        "delete must be parameterized on the TARGET system id only"

    delete_idx = next(i for i, (k, c, _p) in enumerate(calls)
                      if k == "write" and "DETACH DELETE se, f" in c)
    schema_idxs = _schema_write_indices(calls)
    assert schema_idxs, "sanity: the provision wrote a fresh schema"
    assert delete_idx < min(schema_idxs), \
        "the stale-subgraph delete must precede EVERY fresh schema write"


# --------------------------------------------------------------------------- #
# (b) + (d) the delete cypher is scoped to SchemaEntry/Field of that system
# --------------------------------------------------------------------------- #
def test_delete_cypher_scoped_to_system_schema_subgraph_only():
    cypher = onboard._SCHEMA_SUBGRAPH_DELETE_CYPHER
    # Anchored on THIS system's node via its HAS_SCHEMA edge.
    assert "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)" in cypher
    assert "OPTIONAL MATCH (se)-[:HAS_FIELD]->(f:Field)" in cypher
    assert "DETACH DELETE se, f" in cypher
    # Curated / cross-system knowledge must NEVER appear in the delete.
    for forbidden in ("Concept", "Asset", "JoinPath", "IdentityRule",
                      "ValueMapping", "AuthorityRule", "OnboardingLog",
                      "Plant", "MAPS_TO"):
        assert forbidden not in cypher, \
            f"delete statement must not reference {forbidden}"
    # The count (logging) query obeys the same scope.
    count = onboard._SCHEMA_SUBGRAPH_COUNT_CYPHER
    assert "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)" in count
    for forbidden in ("Concept", "Asset", "JoinPath", "IdentityRule",
                      "ValueMapping", "AuthorityRule", "DELETE"):
        assert forbidden not in count


def test_delete_never_targets_other_systems(monkeypatch):
    calls = _install_seams(monkeypatch)
    onboard._do_provision(_body(system_id="mes-mil"))
    deletes = _delete_writes(calls)
    assert len(deletes) == 1
    assert deletes[0][1] == {"sid": "mes-mil"}


# --------------------------------------------------------------------------- #
# (c) delete failure ABORTS the provision — no mixed stale+fresh write
# --------------------------------------------------------------------------- #
def test_delete_failure_aborts_provision_before_any_schema_write(monkeypatch):
    calls = _install_seams(
        monkeypatch, write_returns={"DETACH DELETE se, f": False})
    with pytest.raises(RuntimeError, match="stale schema-subgraph delete"):
        onboard._do_provision(_body())
    assert _schema_write_indices(calls) == [], \
        "a failed cleanup must NOT proceed to write a mixed stale+fresh graph"


# --------------------------------------------------------------------------- #
# Helper unit tests: idempotency, count logging, fail-loud contract
# --------------------------------------------------------------------------- #
def test_helper_idempotent_on_empty_graph(monkeypatch, caplog):
    """First onboard / already-clean graph: count 0, delete matches nothing, no raise."""
    calls = _install_seams(
        monkeypatch,
        query_returns={"RETURN count(DISTINCT se)": [{"entries": 0, "fields": 0}]})
    import logging
    with caplog.at_level(logging.INFO, logger="agents.onboard"):
        removed = onboard._delete_stale_schema_subgraph("sys-a")
    assert removed == 0
    assert len(_delete_writes(calls)) == 1


def test_helper_logs_deletion_counts_at_info(monkeypatch, caplog):
    _install_seams(
        monkeypatch,
        query_returns={"RETURN count(DISTINCT se)": [{"entries": 3, "fields": 17}]})
    import logging
    with caplog.at_level(logging.INFO):
        removed = onboard._delete_stale_schema_subgraph("sys-a")
    assert removed == 20
    assert any("3 SchemaEntry" in r.message and "17 Field" in r.message
               and r.levelno == logging.INFO for r in caplog.records)


def test_helper_raises_on_delete_failure(monkeypatch):
    _install_seams(monkeypatch, write_returns={"DETACH DELETE se, f": False})
    with pytest.raises(RuntimeError, match="sys-a"):
        onboard._delete_stale_schema_subgraph("sys-a")


def test_helper_count_read_is_best_effort(monkeypatch):
    """An unreadable count (query seam returns []) never blocks a healthy delete."""
    _install_seams(monkeypatch)  # queries return [] by default
    removed = onboard._delete_stale_schema_subgraph("sys-a")
    assert removed == 0
