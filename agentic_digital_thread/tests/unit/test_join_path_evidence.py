# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What proved each JoinPath provision writes, kept on the node and returned.

Every provision JoinPath writer records its evidence as NEW properties beside
the unchanged ``jp.method`` (route seed, orchestrator, resolve and
self-learning all key on ``method``):

* ``basis``: ``declared_foreign_key`` (a FOREIGN KEY constraint, on a table or
  on the table behind a one-table view), ``column_name_pattern`` (the ``*_id``
  naming guess), ``value_overlap`` (sampled values shared between two tables),
  ``same_row_identifiers`` (two identifier columns of one row) or
  ``identity_rule`` (a cross-system identity rule);
* the numbers behind it: ``constraint_name``; ``overlap_count`` /
  ``overlap_ratio``; ``pair_count`` or ``unique_constraints``; ``rule_id``
  and ``rule_method``.

The provision result returns those properties for every JoinPath, plus the
system id, the number of mapped fields, the runtime and gateway target names
and the provision time. Offline: graph and database are fakes.
"""
from __future__ import annotations

import time

import pytest

import agents.onboard as onboard
from agents.agent_manager import AgentManager
from agents.models import ProvisionRequest

SID = "sys-a"
SCHEMA = "schema_a"

#: Every ``jp.method`` value the provision writers store. Readers key on the
#: older ones; recording evidence must not change them. A direct identity
#: rule's join says it came from an identity rule (``identity_rule_match``):
#: before 2026-10-19 it was written ``foreign_key_match`` though no foreign
#: key exists, and no reader of a cross-system join keys on that value.
PROVISION_JOIN_METHODS = {
    "foreign_key_match", "value_overlap", "same_row_identifier_set",
    "value_exact_match", "value_format_match", "identity_rule_match",
}


def _ctx(**overrides) -> onboard._ProvisionState:
    body = ProvisionRequest(system_id=SID, system_name="System A", schema=SCHEMA,
                            connection_type="PostgreSQL")
    ctx = onboard._ProvisionState(
        body=body, system_id=SID, system_name="System A", plant="", schema=SCHEMA,
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=[])
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _record_writes(monkeypatch) -> list[tuple[str, dict]]:
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    return writes


def _jp_merges(writes) -> list[tuple[str, dict]]:
    return [(c, p) for c, p in writes if c.startswith("MERGE (jp:JoinPath")]


def _set_value(cypher: str, params: dict, prop: str):
    """The value a JoinPath MERGE SETs on ``jp.<prop>`` (a bound parameter or
    an inline literal), or ``KeyError`` when the statement does not set it."""
    import re
    m = re.search(rf"jp\.{prop} = (\$\w+|'[^']*'|true|false|-?\d+(?:\.\d+)?)", cypher)
    if not m:
        raise KeyError(prop)
    token = m.group(1)
    if token.startswith("$"):
        return params[token[1:]]
    if token.startswith("'"):
        return token[1:-1]
    if token in ("true", "false"):
        return token == "true"
    return float(token) if "." in token else int(token)


# --------------------------------------------------------------------------- #
# b3: declared foreign keys vs the column-name guess
# --------------------------------------------------------------------------- #
def _catalog_pg(*, schema_fks=(), view_fks=(), view_lineage=(), tables=(), id_cols=()):
    """``pg`` answering the internal-join detector's catalog reads by shape."""
    def pg(_schema, sql, params=None):
        if "pg_get_viewdef" in sql:
            return {"rows": [list(r) for r in view_lineage]}
        if "contype = 'f'" in sql and "one_table_views" in sql:
            return {"rows": [list(r) for r in view_fks]}
        if "contype = 'p'" in sql:
            return {"rows": []}
        if "referential_constraints" in sql:
            return {"rows": [list(r) for r in schema_fks]}
        if "information_schema.tables" in sql:
            return {"rows": [[t] for t in tables]}
        if "column_name LIKE" in sql:
            return {"rows": [list(r) for r in id_cols]}
        if "SELECT 1" in sql:
            return {"rows": [[1]]}
        return {"rows": []}
    return pg


class TestInternalJoinBasis:
    def test_a_declared_constraint_is_named_and_marked_declared(self, monkeypatch):
        monkeypatch.setattr(onboard, "pg", _catalog_pg(
            schema_fks=[("table_x", "key_b_id", "table_b", "key_b_id", "fk_table_x_key_b")]))
        joins = onboard._detect_internal_joinpaths(SCHEMA, SID)
        assert joins == [{"from_table": "table_x", "from_field": "key_b_id",
                          "to_table": "table_b", "to_field": "key_b_id",
                          "basis": "declared_foreign_key",
                          "constraint_name": "fk_table_x_key_b"}]

    def test_a_constraint_behind_a_view_is_declared_and_keeps_its_name(self, monkeypatch):
        lineage = [
            ("view_x", "public", "view_x", " SELECT key_b_id\n   FROM public.view_x;", ["key_b_id"]),
            ("view_b", "public", "view_b", " SELECT key_b_id\n   FROM public.view_b;", ["key_b_id"]),
        ]
        monkeypatch.setattr(onboard, "pg", _catalog_pg(
            view_fks=[("view_x", "key_b_id", "view_b", "key_b_id", "fk_base_x_key_b")],
            view_lineage=lineage))
        joins = onboard._detect_internal_joinpaths(SCHEMA, SID)
        assert joins == [{"from_table": "view_x", "from_field": "key_b_id",
                          "to_table": "view_b", "to_field": "key_b_id",
                          "basis": "declared_foreign_key",
                          "constraint_name": "fk_base_x_key_b"}]

    def test_the_naming_guess_is_marked_as_a_name_pattern(self, monkeypatch):
        monkeypatch.setattr(onboard, "pg", _catalog_pg(
            tables=["table_x", "items"], id_cols=[("table_x", "item_id")]))
        joins = onboard._detect_internal_joinpaths(SCHEMA, SID)
        assert joins == [{"from_table": "table_x", "from_field": "item_id",
                          "to_table": "items", "to_field": "item_id",
                          "basis": "column_name_pattern"}]

    def test_the_writer_stores_the_basis_and_constraint_and_keeps_the_method(self, monkeypatch):
        writes = _record_writes(monkeypatch)
        monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a: [
            {"from_table": "table_x", "from_field": "key_b_id", "to_table": "table_b",
             "to_field": "key_b_id", "basis": "declared_foreign_key",
             "constraint_name": "fk_table_x_key_b"},
            {"from_table": "table_x", "from_field": "item_id", "to_table": "items",
             "to_field": "item_id", "basis": "column_name_pattern"},
        ])
        onboard._dp_internal_joinpaths(_ctx())
        merges = _jp_merges(writes)
        assert len(merges) == 2
        (fk_c, fk_p), (guess_c, guess_p) = merges
        assert _set_value(fk_c, fk_p, "method") == "foreign_key_match"
        assert _set_value(fk_c, fk_p, "basis") == "declared_foreign_key"
        assert _set_value(fk_c, fk_p, "constraint_name") == "fk_table_x_key_b"
        assert _set_value(guess_c, guess_p, "method") == "foreign_key_match"
        assert _set_value(guess_c, guess_p, "basis") == "column_name_pattern"
        assert _set_value(guess_c, guess_p, "constraint_name") == ""

    def test_a_detection_without_a_basis_is_never_called_declared(self, monkeypatch):
        writes = _record_writes(monkeypatch)
        monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a: [
            {"from_table": "table_x", "from_field": "key_b_id", "to_table": "table_b",
             "to_field": "key_b_id"}])
        onboard._dp_internal_joinpaths(_ctx())
        (cypher, params), = _jp_merges(writes)
        assert _set_value(cypher, params, "basis") == "column_name_pattern"


# --------------------------------------------------------------------------- #
# b4: value overlap between two tables of the system
# --------------------------------------------------------------------------- #
def test_value_overlap_join_records_its_basis_beside_the_measured_overlap(monkeypatch):
    writes = _record_writes(monkeypatch)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    pair = {"from_table": "table_x", "from_field": "ref_a", "to_table": "table_y",
            "to_field": "ref_a", "transform": "strip_affix", "overlap_count": 1240,
            "overlap_ratio": 0.98}
    assert onboard._dp_write_valueoverlap_pair(_ctx(), pair, set()) is True
    (cypher, params), = _jp_merges(writes)
    assert _set_value(cypher, params, "method") == "value_overlap"
    assert _set_value(cypher, params, "basis") == "value_overlap"
    assert _set_value(cypher, params, "overlap_count") == 1240
    assert _set_value(cypher, params, "overlap_ratio") == 0.98


# --------------------------------------------------------------------------- #
# b5: identifier columns of the same row
# --------------------------------------------------------------------------- #
class TestSameRowBasis:
    def _run(self, monkeypatch, *, stats, unique=()):
        writes = _record_writes(monkeypatch)
        monkeypatch.setattr(onboard, "_dp_samerow_measure_table",
                            lambda _ctx, _tbl, pairs: ("ok", {p: stats for p in pairs}))
        members = ["code_a", "code_b"]
        col_concept = {c: "thing-id" for c in members if c not in unique}
        onboard._dp_samerow_table(_ctx(), "table_x", col_concept, set(unique), members)
        return _jp_merges(writes)

    def test_a_measured_pair_records_how_many_value_pairs_read_one_to_one(self, monkeypatch):
        (cypher, params), = self._run(monkeypatch, stats=(412, 412, 412))
        assert _set_value(cypher, params, "method") == "same_row_identifier_set"
        assert _set_value(cypher, params, "basis") == "same_row_identifiers"
        assert _set_value(cypher, params, "pair_count") == 412
        assert _set_value(cypher, params, "unique_constraints") is False

    def test_a_both_unique_pair_records_the_constraints_not_a_count(self, monkeypatch):
        (cypher, params), = self._run(monkeypatch, stats=(1, 2, 3),
                                      unique=("code_a", "code_b"))
        assert _set_value(cypher, params, "method") == "same_row_identifier_set"
        assert _set_value(cypher, params, "basis") == "same_row_identifiers"
        assert _set_value(cypher, params, "unique_constraints") is True
        assert "jp.pair_count" not in cypher


# --------------------------------------------------------------------------- #
# c2: joins from cross-system identity rules
# --------------------------------------------------------------------------- #
class TestIdentityRuleBasis:
    def _write(self, monkeypatch, rule):
        writes = _record_writes(monkeypatch)
        onboard._dp_write_rule_joinpath(_ctx(), rule, "table_x", "ref_a",
                                        "sys-b", "table_z", "ref_z")
        (cypher, params), = _jp_merges(writes)
        return cypher, params

    def test_a_direct_rule_names_the_identity_rule_as_its_method(self, monkeypatch):
        """Walk 2026-10-19: a kept identity rule's join read ``foreign_key_match``
        in the graph, though it came from an identity rule, not a key."""
        rule = {"concept": "thing-id", "source_field": "table_x.ref_a",
                "target_system": "sys-b", "target_field": "table_z.ref_z"}
        cypher, params = self._write(monkeypatch, rule)
        assert _set_value(cypher, params, "method") == "identity_rule_match"
        assert _set_value(cypher, params, "transform") == "direct"
        assert _set_value(cypher, params, "basis") == "identity_rule"
        digest = onboard._rule_digest_for(SID, rule)
        assert _set_value(cypher, params, "rule_id") == f"ir-{SID}-{digest}"
        assert _set_value(cypher, params, "rule_method") == ""
        assert "jp.overlap_count" not in cypher

    def test_an_exact_agreement_rule_records_its_shared_value_count(self, monkeypatch):
        rule = {"concept": "thing-id", "source_field": "table_x.ref_a",
                "target_system": "sys-b", "target_field": "table_z.ref_z",
                "method": "value_exact_match", "transform": "identity",
                "shared_count": 57, "reasoning": "57 exact values"}
        cypher, params = self._write(monkeypatch, rule)
        assert _set_value(cypher, params, "method") == "value_exact_match"
        assert _set_value(cypher, params, "basis") == "identity_rule"
        assert _set_value(cypher, params, "rule_method") == "value_exact_match"
        assert _set_value(cypher, params, "overlap_count") == 57

    def test_a_pattern_rule_shared_count_is_not_recorded_as_measured_evidence(self, monkeypatch):
        """A non-exact rule is model- or pattern-derived; its ``shared_count``
        is untrusted and must never become displayed overlap evidence. Only an
        exact-agreement rule's count is measured."""
        rule = {"concept": "thing-id", "source_field": "table_x.ref_a",
                "target_system": "sys-b", "target_field": "table_z.ref_z",
                "source_pattern": "A-{n}", "target_pattern": "{n}",
                "shared_count": 99}
        cypher, params = self._write(monkeypatch, rule)
        assert _set_value(cypher, params, "method") == "value_format_match"
        assert "jp.overlap_count" not in cypher

    def test_a_model_proposed_rule_with_no_method_and_a_shared_count_records_none(self, monkeypatch):
        """A rule the onboarding LLM proposed (no measured method) carrying an
        invented ``shared_count`` must not have it stored as overlap evidence."""
        rule = {"concept": "thing-id", "source_field": "table_x.ref_a",
                "target_system": "sys-b", "target_field": "table_z.ref_z",
                "shared_count": 1000}
        cypher, params = self._write(monkeypatch, rule)
        assert _set_value(cypher, params, "method") == "identity_rule_match"
        assert "jp.overlap_count" not in cypher


def test_every_provision_writer_keeps_a_method_readers_already_know(monkeypatch):
    """Recording evidence adds properties; no writer's ``method`` changes."""
    writes = _record_writes(monkeypatch)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a: [
        {"from_table": "t1", "from_field": "a_id", "to_table": "t2", "to_field": "a_id",
         "basis": "declared_foreign_key", "constraint_name": "fk"}])
    monkeypatch.setattr(onboard, "_dp_samerow_measure_table",
                        lambda _ctx, _tbl, pairs: ("ok", {p: (5, 5, 5) for p in pairs}))
    ctx = _ctx()
    onboard._dp_internal_joinpaths(ctx)
    onboard._dp_write_valueoverlap_pair(ctx, {
        "from_table": "t1", "from_field": "r", "to_table": "t3", "to_field": "r",
        "overlap_count": 4, "overlap_ratio": 0.5}, set())
    onboard._dp_samerow_table(ctx, "t1", {"c1": "k", "c2": "k"}, set(), ["c1", "c2"])
    for rule in ({"concept": "k", "source_field": "c1", "target_system": "sys-b",
                  "target_field": "c9"},
                 {"concept": "k", "source_field": "c1", "target_system": "sys-b",
                  "target_field": "c8", "source_pattern": "A-{n}", "target_pattern": "{n}"},
                 {"concept": "k", "source_field": "c1", "target_system": "sys-b",
                  "target_field": "c7", "method": "value_exact_match", "shared_count": 3}):
        onboard._dp_write_rule_joinpath(ctx, rule, "t1", "c1", "sys-b", "t9", rule["target_field"])
    merges = _jp_merges(writes)
    assert len(merges) == 6
    methods = {_set_value(c, p, "method") for c, p in merges}
    assert methods <= PROVISION_JOIN_METHODS
    assert {_set_value(c, p, "basis") for c, p in merges} == {
        "declared_foreign_key", "value_overlap", "same_row_identifiers", "identity_rule"}


def test_the_readers_that_key_on_foreign_key_match_read_joins_inside_one_system():
    """The identity rule's join crosses two systems. The two readers that key
    on ``foreign_key_match`` (the identifier-forms pairing and the metric
    resolver's mirror collapse) read only joins inside one system, so the
    method a cross-system join carries changes neither."""
    from agents import identifier_translation, metric_dimension
    assert ("jp.from_system = $sid AND jp.to_system = $sid"
            in metric_dimension._JOINPATH_CYPHER)
    for method in ("foreign_key_match", "identity_rule_match"):
        cross = {"id": "jp-1", "method": method, "from_system": "sys-a", "to_system": "sys-b",
                 "from_field": "schema_a.t1.ref", "to_field": "schema_b.t2.ref"}
        assert identifier_translation._pairing_edges(
            "sys-a", {"schema_a.t1", "schema_b.t2"}, [cross]) == {}
    inside = {"id": "jp-2", "method": "foreign_key_match", "from_system": "sys-a",
              "to_system": "sys-a", "from_field": "schema_a.t1.ref", "to_field": "schema_a.t2.ref"}
    assert identifier_translation._pairing_edges(
        "sys-a", {"schema_a.t1", "schema_a.t2"}, [inside])


# --------------------------------------------------------------------------- #
# The provision result
# --------------------------------------------------------------------------- #
def test_written_join_paths_return_their_evidence(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda cypher, params=None: seen.append(cypher) or [])
    onboard._dp_written_artifacts(SID)
    jp_query = seen[0]
    returned = set(jp_query.split("RETURN", 1)[1].replace(" ", "").split(","))
    for field in ("basis", "constraint_name", "overlap_count", "overlap_ratio",
                  "pair_count", "unique_constraints", "rule_id", "rule_method", "transform"):
        assert f"jp.{field}AS{field}" in returned, field


class _CloudManager:
    mode = "cloud"
    gateway_id = "gw-1"


def _result(monkeypatch, *, manager=None, mapped_rows=None, started_ago=None):
    def fake_query(cypher, params=None):
        if "MAPS_TO" in cypher:
            return mapped_rows if mapped_rows is not None else []
        return []
    monkeypatch.setattr(onboard, "neptune_query", fake_query)
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    if manager is not None:
        monkeypatch.setattr(onboard, "_agent_manager", manager)
    ctx = _ctx(mcp_url="https://gateway.example/mcp", tool_name="query_sys_a")
    if started_ago is not None:
        ctx.started_at = time.monotonic() - started_ago
    return onboard._dp_result(ctx)


def test_the_result_names_the_system_runtime_and_gateway_target(monkeypatch):
    result = _result(monkeypatch, manager=_CloudManager())
    assert result["system_id"] == SID
    assert result["runtime_name"] == AgentManager._runtime_name(SID)
    assert result["gateway_target"] == AgentManager._gateway_target_name(SID)


def test_a_local_agent_gets_no_runtime_or_gateway_name(monkeypatch):
    class _Local:
        mode = "local"
        gateway_id = ""
    result = _result(monkeypatch, manager=_Local())
    assert "runtime_name" not in result and "gateway_target" not in result


def test_the_result_counts_the_mapped_fields_it_reads_back(monkeypatch):
    result = _result(monkeypatch, mapped_rows=[{"fields": 42}])
    assert result["mapped_fields"] == 42


def test_an_unreadable_mapped_field_count_is_left_out_not_zero(monkeypatch):
    result = _result(monkeypatch, mapped_rows=[])
    assert "mapped_fields" not in result


def test_the_result_carries_the_provision_time(monkeypatch):
    result = _result(monkeypatch, started_ago=12.4)
    assert result["provision_seconds"] == pytest.approx(12, abs=1)


def test_the_provision_state_starts_its_clock(monkeypatch):
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    before = time.monotonic()
    ctx = onboard._make_provision_state(ProvisionRequest(system_id=SID))
    assert before <= ctx.started_at <= time.monotonic()
