# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for onboarding-time intra-system capture (ADR-0041, G1).

Two additive behaviors in ``onboard.py``:

* **G1a** — ``_fk_target_candidates``: the FK pluralization heuristic also
  matches snake_case tables ENDING in the pluralized prefix, so
  ``production_lines`` is discoverable from ``line_id`` (the miss that left
  the MES ``downtime_events.line_id → production_lines`` link uncaptured).
* **G1b** — provision step b4: deterministic transform-ladder pairs returned by
  ``join_discovery.discover_intra_system`` are written DIRECTLY to the graph as
  internal JoinPaths (``method='value_overlap'``, transform + measured overlap
  as provenance), extending b3's ``foreign_key_match`` direct-write precedent.
  FK wins on duplicates; a raising discovery never breaks provision.

Pure in-process: ``_do_provision`` is driven directly with every impure seam
monkeypatched at the module-global level (conventions of
``test_provision_entity_edge.py``) — no AWS, no Neptune, no Postgres, no LLM.
"""
from __future__ import annotations

import pytest

import agents.join_discovery as join_discovery
import agents.onboard as onboard
from agents.catalog import catalog
from agents.models import ProvisionRequest


@pytest.fixture(autouse=True)
def _seed_live_catalog_with_demo_systems():
    """Base mode ships an EMPTY live catalog (ADR-0030); seed the shared catalog
    from the test-local demo fixture, then restore (see test_provision_entity_edge)."""
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


# --------------------------------------------------------------------------- #
# Fakes / seams (mirrors test_provision_entity_edge.py)
# --------------------------------------------------------------------------- #
class _FakeAgentManager:
    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeSSM:
    def get_parameter(self, **_kwargs):
        raise RuntimeError("no SSM in unit tests")


def _install_seams(monkeypatch):
    """Neutralize every impure seam in the PostgreSQL provision path; return the
    recorded ``neptune_write(cypher, params)`` list."""
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, p or {})))
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
    # Keep entity resolution out of scope — b4/b5 behavior is what's under test.
    monkeypatch.setattr(onboard, "_classify_table_entity",
                        lambda *_a, **_k: {"entity": None, "mint": None})
    return writes


def _mapping(table: str, column: str, concept: str) -> dict:
    return {
        "table": table,
        "field": f"{table}.{column}",
        "concept": concept,
        "mappings": [{"concept": concept, "role": "identifier", "confidence": 0.9}],
    }


def _value_overlap_writes(writes) -> list[tuple[str, dict]]:
    return [(c, p) for c, p in writes if "jp.method = 'value_overlap'" in c]


_LINE_PAIR = {
    "from_table": "downtime_events", "from_field": "line_id",
    "to_table": "production_lines", "to_field": "line_id",
    "transform": "strip_affix", "overlap_count": 7, "overlap_ratio": 0.875,
    "examples": ["LINE1"],
}


# --------------------------------------------------------------------------- #
# G1a — _fk_target_candidates (pure)
# --------------------------------------------------------------------------- #
class TestFkTargetCandidates:
    def test_suffix_match_finds_production_lines_from_line_prefix(self):
        cands = onboard._fk_target_candidates(
            "line", {"production_lines", "work_orders"})
        assert "production_lines" in cands

    def test_exact_plural_forms_keep_priority_over_suffix_matches(self):
        cands = onboard._fk_target_candidates(
            "line", {"lines", "production_lines"})
        assert cands == ["lines", "production_lines"]

    def test_only_present_tables_are_returned(self):
        assert onboard._fk_target_candidates("order", {"downtime_events"}) == []

    def test_empty_prefix_yields_nothing(self):
        assert onboard._fk_target_candidates("", {"production_lines"}) == []

    def test_no_underscore_boundary_no_match(self):
        # "outlines" ends in "lines" but NOT in "_lines" — the snake_case
        # boundary is required, so it only matches as… nothing here.
        assert onboard._fk_target_candidates("line", {"outlines"}) == []

    def test_suffix_matches_are_sorted_deterministically(self):
        cands = onboard._fk_target_candidates(
            "line", {"production_lines", "assembly_lines"})
        assert cands == ["assembly_lines", "production_lines"]


# --------------------------------------------------------------------------- #
# G1a — heuristic wiring in _detect_internal_joinpaths
# --------------------------------------------------------------------------- #
class TestDetectInternalJoinpathsPluralization:
    def _fake_pg(self, has_column: bool):
        def fake_pg(schema, sql, params=None):
            if "referential_constraints" in sql:
                return {"rows": []}
            if "information_schema.tables" in sql:
                return {"rows": [["downtime_events"], ["production_lines"],
                                 ["work_orders"]]}
            if "SELECT 1" in sql:
                # column-existence check on the candidate table
                if has_column and params[1] == "production_lines":
                    return {"rows": [[1]]}
                return {"rows": []}
            if "column_name LIKE" in sql:
                return {"rows": [["downtime_events", "line_id"]]}
            return {"rows": []}
        return fake_pg

    def test_line_id_resolves_to_production_lines(self, monkeypatch):
        monkeypatch.setattr(onboard, "pg", self._fake_pg(has_column=True))
        joins = onboard._detect_internal_joinpaths("mes_detroit", "mes-det")
        assert {"from_table": "downtime_events", "from_field": "line_id",
                "to_table": "production_lines", "to_field": "line_id",
                "basis": "column_name_pattern"} in joins

    def test_column_existence_check_still_required(self, monkeypatch):
        monkeypatch.setattr(onboard, "pg", self._fake_pg(has_column=False))
        joins = onboard._detect_internal_joinpaths("mes_detroit", "mes-det")
        assert joins == []

    def test_reverse_suffix_match_dedupes_to_first_orientation(self, monkeypatch):
        def fake_pg(_schema, sql, params=None):
            if "referential_constraints" in sql:
                return {"rows": []}
            if "information_schema.tables" in sql:
                return {"rows": [["item_suppliers"], ["suppliers"]]}
            if "column_name LIKE" in sql:
                return {"rows": [
                    ["item_suppliers", "supplier_id"],
                    ["suppliers", "supplier_id"],
                ]}
            if "SELECT 1" in sql:
                return {"rows": [[1]]}
            return {"rows": []}

        monkeypatch.setattr(onboard, "pg", fake_pg)
        joins = onboard._detect_internal_joinpaths("supply_data", "supply-system")

        assert joins == [{
            "from_table": "item_suppliers", "from_field": "supplier_id",
            "to_table": "suppliers", "to_field": "supplier_id",
            "basis": "column_name_pattern",
        }]


# --------------------------------------------------------------------------- #
# Reprovision replaces only auto-generated intra-system JoinPaths
# --------------------------------------------------------------------------- #
class TestProvisionAutoJoinPathReplacement:
    def test_stale_reverse_removed_canonical_rebuilt_and_confirmed_survives(
        self, monkeypatch
    ):
        _install_seams(monkeypatch)
        system_id = "supply-system"
        stale_reverse = {
            "id": "jp-internal-supply-system-suppliers-supplier_id-item_suppliers",
            "from_system": system_id, "to_system": system_id,
            "from_table": "supply_data.suppliers", "from_field": "supplier_id",
            "to_table": "supply_data.item_suppliers", "to_field": "supplier_id",
            "transform": "direct", "method": "foreign_key_match",
        }
        user_confirmed = {
            "id": "jp-internal-supply-system-confirmed",
            "from_system": system_id, "to_system": system_id,
            "from_table": "supply_data.suppliers", "from_field": "supplier_id",
            "to_table": "supply_data.item_suppliers", "to_field": "supplier_id",
            "transform": "direct", "method": "user_confirmed",
        }
        cross_system = {
            "id": "jp-internal-supply-system-cross",
            "from_system": system_id, "to_system": "other-system",
            "from_table": "supply_data.suppliers", "from_field": "supplier_id",
            "to_table": "other_data.suppliers", "to_field": "supplier_id",
            "transform": "direct", "method": "foreign_key_match",
        }
        graph = [stale_reverse, user_confirmed, cross_system]
        calls: list[tuple[str, dict]] = []

        def fake_write(cypher, params=None):
            params = params or {}
            calls.append((cypher, params))
            if cypher == onboard._AUTO_INTERNAL_JOINPATH_DELETE_CYPHER:
                prefixes = ("jp-internal-", "jp-vo-", "jp-samerow-")
                graph[:] = [jp for jp in graph if not (
                    jp["from_system"] == params["sid"]
                    and jp["to_system"] == params["sid"]
                    and jp["id"].startswith(prefixes)
                    and jp["method"] != "user_confirmed"
                )]
            elif "jp.method = 'foreign_key_match'" in cypher:
                graph.append({
                    "id": params["jpid"],
                    "from_system": params["sys"], "to_system": params["sys"],
                    "from_table": params["ftbl"], "from_field": params["ffld"],
                    "to_table": params["ttbl"], "to_field": params["tfld"],
                    "transform": "direct", "method": "foreign_key_match",
                })
            return True

        monkeypatch.setattr(onboard, "neptune_write", fake_write)
        monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a: [{
            "from_table": "item_suppliers", "from_field": "supplier_id",
            "to_table": "suppliers", "to_field": "supplier_id",
        }])
        monkeypatch.setattr(join_discovery, "discover_intra_system",
                            lambda _sid: {"pairs": []})
        catalog._systems.append({
            "id": system_id, "name": "Supply System", "plant": "All Sites",
            "system_type": "Purchasing", "connection_type": "PostgreSQL",
            "schema": "supply_data", "status": "registered",
        })
        body = ProvisionRequest(
            system_id=system_id, system_name="Supply System", schema="supply_data",
            connection_type="PostgreSQL",
            mappings=[
                _mapping("item_suppliers", "supplier_id", "supplier-id"),
                _mapping("suppliers", "supplier_id", "supplier-id"),
            ],
        )
        onboard._do_provision(body)

        generated = [jp for jp in graph
                     if jp["from_system"] == system_id
                     and jp["to_system"] == system_id
                     and jp["method"] != "user_confirmed"]
        assert generated == [{
            "id": "jp-internal-supply-system-item_suppliers-supplier_id-suppliers",
            "from_system": system_id, "to_system": system_id,
            "from_table": "supply_data.item_suppliers", "from_field": "supplier_id",
            "to_table": "supply_data.suppliers", "to_field": "supplier_id",
            "transform": "direct", "method": "foreign_key_match",
        }]
        assert user_confirmed in graph
        assert cross_system in graph

        cleanup_idx = next(i for i, (c, _p) in enumerate(calls)
                           if c == onboard._AUTO_INTERNAL_JOINPATH_DELETE_CYPHER)
        rebuild_idx = next(i for i, (c, _p) in enumerate(calls)
                           if "jp.method = 'foreign_key_match'" in c)
        assert cleanup_idx < rebuild_idx
        cleanup = onboard._AUTO_INTERNAL_JOINPATH_DELETE_CYPHER
        assert "jp.from_system = $sid AND jp.to_system = $sid" in cleanup
        assert "coalesce(jp.method, '') <> 'user_confirmed'" in cleanup
        for prefix in ("jp-internal-", "jp-vo-", "jp-samerow-"):
            assert prefix in cleanup


# --------------------------------------------------------------------------- #
# G1b — provision step b4 (direct-write value-overlap JoinPaths)
# --------------------------------------------------------------------------- #
class TestProvisionValueOverlapCapture:
    def test_transformable_pair_writes_one_value_overlap_joinpath(self, monkeypatch):
        writes, _ = self._provision(monkeypatch, pairs=[dict(_LINE_PAIR)])
        vo = _value_overlap_writes(writes)
        assert len(vo) == 1
        cypher, params = vo[0]
        assert params["transform"] == "strip_affix"
        assert params["ocount"] == 7
        assert params["oratio"] == pytest.approx(0.875)
        assert params["ftbl"] == "mes_detroit.downtime_events"
        assert params["ttbl"] == "mes_detroit.production_lines"
        assert params["sys"] == "mes-det"
        # provenance properties are set on the node itself
        assert "jp.overlap_count" in cypher and "jp.overlap_ratio" in cypher
        # Distinct id prefix from b3's FK ids (jp-internal-…) — the two methods
        # never MERGE onto each other's nodes (F-C).
        jpid = params["jpid"]
        assert jpid == ("jp-vo-mes-det-downtime_events-line_id-"
                        "production_lines-line_id")
        # FROM_SYSTEM / TO_SYSTEM edges accompany the node (b3 idiom)
        edges = [p for c, p in writes
                 if "FROM_SYSTEM" in c or "TO_SYSTEM" in c]
        assert sum(1 for p in edges if p.get("jpid") == jpid) == 2

    def test_discovery_raising_never_breaks_provision(self, monkeypatch):
        writes = _install_seams(monkeypatch)

        def _boom(_sid):
            raise RuntimeError("sampling exploded")

        monkeypatch.setattr(join_discovery, "discover_intra_system", _boom)
        body = ProvisionRequest(
            system_id="mes-det", system_name="MES-DET", schema="mes_detroit",
            connection_type="PostgreSQL",
            mappings=[_mapping("downtime_events", "line_id", "line-id")],
        )
        prior = catalog.get("mes-det")["status"]
        try:
            result = onboard._do_provision(body)
        finally:
            catalog.update_status("mes-det", prior)
        assert "error" not in (result or {})
        assert _value_overlap_writes(writes) == []

    def test_fk_detected_pair_wins_value_overlap_duplicate_skipped(self, monkeypatch):
        fk_pair = [{"from_table": "downtime_events", "from_field": "line_id",
                    "to_table": "production_lines", "to_field": "line_id"}]
        writes, _ = self._provision(
            monkeypatch, pairs=[dict(_LINE_PAIR)], fk_joins=fk_pair)
        assert _value_overlap_writes(writes) == []
        fk_writes = [c for c, _p in writes if "foreign_key_match" in c]
        assert len(fk_writes) == 1

    def test_reversed_fk_duplicate_also_skipped(self, monkeypatch):
        fk_pair = [{"from_table": "production_lines", "from_field": "line_id",
                    "to_table": "downtime_events", "to_field": "line_id"}]
        writes, _ = self._provision(
            monkeypatch, pairs=[dict(_LINE_PAIR)], fk_joins=fk_pair)
        assert _value_overlap_writes(writes) == []

    def test_prior_graph_joinpath_skips_the_merge(self, monkeypatch):
        """Cross-run dedupe (F-C): a JoinPath already in the graph on the same
        (from_table, from_field, to_table) — e.g. a prior run's FK write —
        wins, and b4 skips the MERGE."""
        writes = self._provision_with_prior(
            monkeypatch, prior_jpid="jp-internal-mes-det-downtime_events-line_id-production_lines")
        assert _value_overlap_writes(writes) == []

    def test_prior_graph_joinpath_reverse_direction_also_skips(self, monkeypatch):
        """The dedupe read matches either direction — a prior JoinPath written
        production_lines → downtime_events also suppresses the b4 write."""
        reads: list[tuple[str, dict]] = []

        def _fake_query(cypher, params=None):
            if "AS jpid" in cypher:
                reads.append((cypher, params or {}))
                return [{"jpid": "jp-internal-mes-det-production_lines-line_id-downtime_events"}]
            return []

        writes = _install_seams(monkeypatch)
        monkeypatch.setattr(onboard, "neptune_query", _fake_query)
        monkeypatch.setattr(join_discovery, "discover_intra_system",
                            lambda _sid: {"pairs": [dict(_LINE_PAIR)]})
        self._run_provision()
        assert _value_overlap_writes(writes) == []
        # ONE parameterized read per candidate pair, carrying both directions.
        assert len(reads) == 1
        cypher, params = reads[0]
        assert "OR (jp.from_table = $ttbl" in cypher
        assert params == {
            "sys": "mes-det",
            "ftbl": "mes_detroit.downtime_events", "ffld": "line_id",
            "ttbl": "mes_detroit.production_lines", "tfld": "line_id",
        }

    def test_invalid_identifier_in_pair_is_skipped(self, monkeypatch):
        bad = dict(_LINE_PAIR)
        bad["to_table"] = "production lines, extra"
        writes, _ = self._provision(monkeypatch, pairs=[bad])
        assert _value_overlap_writes(writes) == []

    def test_empty_discovery_writes_nothing(self, monkeypatch):
        writes, _ = self._provision(monkeypatch, pairs=[])
        assert _value_overlap_writes(writes) == []

    # ------------------------------------------------------------------ #
    def _run_provision(self):
        body = ProvisionRequest(
            system_id="mes-det", system_name="MES-DET", schema="mes_detroit",
            connection_type="PostgreSQL",
            mappings=[_mapping("downtime_events", "line_id", "line-id"),
                      _mapping("production_lines", "line_id", "line-id")],
        )
        prior = catalog.get("mes-det")["status"]
        try:
            result = onboard._do_provision(body)
        finally:
            catalog.update_status("mes-det", prior)
        return result

    def _provision_with_prior(self, monkeypatch, *, prior_jpid: str):
        writes = _install_seams(monkeypatch)

        def _fake_query(cypher, params=None):
            # The b4 dedupe read aliases `RETURN jp.id AS jpid` — the only
            # provision-path query that does.
            if "AS jpid" in cypher:
                return [{"jpid": prior_jpid}]
            return []

        monkeypatch.setattr(onboard, "neptune_query", _fake_query)
        monkeypatch.setattr(join_discovery, "discover_intra_system",
                            lambda _sid: {"pairs": [dict(_LINE_PAIR)]})
        self._run_provision()
        return writes

    def _provision(self, monkeypatch, *, pairs, fk_joins=None):
        writes = _install_seams(monkeypatch)
        if fk_joins is not None:
            monkeypatch.setattr(onboard, "_detect_internal_joinpaths",
                                lambda *_a, **_k: list(fk_joins))
        monkeypatch.setattr(join_discovery, "discover_intra_system",
                            lambda _sid: {"pairs": list(pairs)})
        result = self._run_provision()
        return writes, result
