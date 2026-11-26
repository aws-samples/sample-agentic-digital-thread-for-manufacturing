# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for same-row identifier sets at provision (ADR-0041, G2).

Interchangeable identifiers ON ONE ROW of a table (the CMMS
``assets.serial_number`` / ``asset_tag`` / ``asset_id`` case) become internal
JoinPath nodes at provision — step b5 of ``_do_provision``:

* ``from_table == to_table``, ``transform='same_row'``,
  ``method='same_row_identifier_set'`` (ADR-0041 D1 — NOT a widened
  ``IDENTIFIED_BY``; that edge stays the authoritative PRIMARY identity key).
* Membership evidence is deterministic: identifier-like (never measure-like)
  columns pair when both map to the SAME concept via this provision's mappings,
  or when BOTH carry a DDL UNIQUE constraint (either-unique would write false
  edges — see ``test_single_unique_column_never_pairs_with_nonunique_identifiers``).
* Node ids sort the column pair (``jp-samerow-<sys>-<tbl>-<colA>-<colB>``) so
  re-onboarding MERGEs idempotently.

Pure in-process: seams monkeypatched per ``test_provision_entity_edge.py``.
"""
from __future__ import annotations

import random
import re
from types import SimpleNamespace

import pytest

import agents.join_discovery as join_discovery
import agents.onboard as onboard
from agents.catalog import catalog
from agents.models import ProvisionRequest


@pytest.fixture(autouse=True)
def _seed_live_catalog_with_demo_systems():
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


def _install_seams(monkeypatch, *, unique_rows=None, unique_read_raises=False,
                  pair_stats=None, measured=None, table_status=None,
                  unique_read_unavailable=False):
    """Neutralize the impure provision seams; ``unique_rows`` feeds the b5 DDL
    UNIQUE read (``[(table, column, constraint_name), ...]`` — the
    constraint_name column makes COMPOSITE constraints structurally testable).
    ``unique_read_raises`` makes that read raise (the S-D degraded-read case).
    ``pair_stats`` answers the b5 1:1 measurement of same-concept pairs
    (``onboard._dp_samerow_measure_table``, whose own SQL is tested in
    ``TestSameRowMeasureRead``): ``{(table, col_a, col_b): (distinct_a,
    distinct_b, distinct_pairs)}``; an unlisted pair reads 1:1 on 10 rows.
    ``table_status`` makes a table's read come back ``"failed"`` or
    ``"too_large"``. ``measured`` (a list) records each read as
    ``(table, [(col_a, col_b), ...])``. Returns the recorded writes."""
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub system prompt")
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeSSM())
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "_classify_table_entity",
                        lambda *_a, **_k: {"entity": None, "mint": None})
    # b4 discovery out of scope here — return no pairs.
    monkeypatch.setattr(join_discovery, "discover_intra_system",
                        lambda _sid: {"pairs": []})

    rows = [list(r) for r in (unique_rows or [])]
    stats = dict(pair_stats or {})
    statuses = dict(table_status or {})

    def fake_pg(schema, sql, params=None):
        if "UNIQUE" in sql:
            if unique_read_raises:
                raise RuntimeError("info_schema unavailable")
            if unique_read_unavailable:
                # pg()'s own failure envelope (agents/db.py).
                return {"columns": [], "rows": [], "row_count": 0,
                        "unavailable": True, "error_category": "OperationalError"}
            return {"rows": rows}
        return {"rows": []}

    def fake_measure_table(ctx, tbl, pairs):
        if measured is not None:
            measured.append((tbl, list(pairs)))
        status = statuses.get(tbl, "ok")
        if status != "ok":
            return status, {}
        return "ok", {(a, b): stats.get((tbl, a, b), (10, 10, 10)) for a, b in pairs}

    monkeypatch.setattr(onboard, "pg", fake_pg)
    monkeypatch.setattr(onboard, "_dp_samerow_measure_table", fake_measure_table)
    return writes


def _mapping(table: str, column: str, concept: str) -> dict:
    return {
        "table": table,
        "field": f"{table}.{column}",
        "concept": concept,
        "mappings": [{"concept": concept, "role": "identifier", "confidence": 0.9}],
    }


def _run(monkeypatch, *, mappings, unique_rows=None, system_id="cmms",
         schema="cmms_maintenance", unique_read_raises=False,
         want_result=False, pair_stats=None, measured=None, table_status=None,
         unique_read_unavailable=False):
    writes = _install_seams(monkeypatch, unique_rows=unique_rows,
                            unique_read_raises=unique_read_raises,
                            pair_stats=pair_stats, measured=measured,
                            table_status=table_status,
                            unique_read_unavailable=unique_read_unavailable)
    entry = catalog.get(system_id)
    assert entry is not None, f"expected seed system '{system_id}'"
    prior = entry["status"]
    body = ProvisionRequest(
        system_id=system_id, system_name=system_id.upper(), schema=schema,
        connection_type="PostgreSQL", mappings=mappings,
    )
    try:
        result = onboard._do_provision(body)
    finally:
        catalog.update_status(system_id, prior)
    return (writes, result) if want_result else writes


def _same_row_writes(writes) -> list[tuple[str, dict]]:
    return [(c, p) for c, p in writes
            if "jp.method = 'same_row_identifier_set'" in c]


class TestSameRowIdentifierSets:
    def test_shared_concept_set_writes_a_joinpath_per_pair(self, monkeypatch):
        writes = _run(monkeypatch, mappings=[
            _mapping("assets", "serial_number", "equipment-id"),
            _mapping("assets", "asset_tag", "equipment-id"),
            _mapping("assets", "asset_id", "equipment-id"),
        ])
        sr = _same_row_writes(writes)
        ids = sorted(p["jpid"] for _c, p in sr)
        assert ids == [
            "jp-samerow-cmms-assets-asset_id-asset_tag",
            "jp-samerow-cmms-assets-asset_id-serial_number",
            "jp-samerow-cmms-assets-asset_tag-serial_number",
        ]
        for cypher, params in sr:
            assert "jp.transform = 'same_row'" in cypher
            # from_table == to_table — the ADR-0041 D1 shape
            assert "jp.from_table = $tbl" in cypher and "jp.to_table = $tbl" in cypher
            assert params["tbl"] == "cmms_maintenance.assets"
            assert params["sys"] == "cmms"

    def test_ids_are_idempotent_regardless_of_mapping_order(self, monkeypatch):
        writes_fwd = _run(monkeypatch, mappings=[
            _mapping("assets", "serial_number", "equipment-id"),
            _mapping("assets", "asset_tag", "equipment-id"),
        ])
        writes_rev = _run(monkeypatch, mappings=[
            _mapping("assets", "asset_tag", "equipment-id"),
            _mapping("assets", "serial_number", "equipment-id"),
        ])
        ids_fwd = sorted(p["jpid"] for _c, p in _same_row_writes(writes_fwd))
        ids_rev = sorted(p["jpid"] for _c, p in _same_row_writes(writes_rev))
        assert ids_fwd == ids_rev == [
            "jp-samerow-cmms-assets-asset_tag-serial_number"]

    def test_ddl_unique_columns_join_the_set_without_a_shared_concept(self, monkeypatch):
        # serial_number is concept-mapped; asset_tag is NOT in the mappings at
        # all — but BOTH carry DDL UNIQUE constraints, the alternate evidence
        # (ADR-0041 D1: both-unique, never either-unique).
        writes = _run(
            monkeypatch,
            mappings=[_mapping("assets", "serial_number", "equipment-id")],
            unique_rows=[("assets", "asset_tag", "uq_assets_asset_tag"),
                         ("assets", "serial_number", "uq_assets_serial_number")],
        )
        sr = _same_row_writes(writes)
        assert [p["jpid"] for _c, p in sr] == [
            "jp-samerow-cmms-assets-asset_tag-serial_number"]
        _c, params = sr[0]
        assert (params["ffld"], params["tfld"]) == ("asset_tag", "serial_number")

    def test_single_unique_column_never_pairs_with_nonunique_identifiers(self, monkeypatch):
        # The F-A false-edge case: wo_number is DDL-UNIQUE but line_id is not,
        # and they share no concept — either-unique pairing would have written
        # a wrong interchangeability (same_row) claim here. Both-unique writes
        # nothing.
        writes = _run(
            monkeypatch,
            mappings=[_mapping("work_orders", "line_id", "line-id")],
            unique_rows=[("work_orders", "wo_number", "uq_wo_number")],
        )
        assert _same_row_writes(writes) == []

    def test_single_identifier_table_writes_nothing(self, monkeypatch):
        writes = _run(monkeypatch, mappings=[
            _mapping("assets", "asset_id", "equipment-id"),
        ])
        assert _same_row_writes(writes) == []

    def test_measure_like_columns_never_enter_a_set(self, monkeypatch):
        # reject_quantity shares the concept AND is DDL-UNIQUE — both evidence
        # paths must still exclude it (measure gate beats everything).
        writes = _run(
            monkeypatch,
            mappings=[
                _mapping("assets", "serial_number", "equipment-id"),
                _mapping("assets", "reject_quantity", "equipment-id"),
            ],
            unique_rows=[("assets", "reject_quantity", "uq_reject_quantity")],
        )
        assert _same_row_writes(writes) == []

    def test_different_concepts_without_unique_evidence_write_nothing(self, monkeypatch):
        writes = _run(monkeypatch, mappings=[
            _mapping("assets", "serial_number", "serial-number"),
            _mapping("assets", "asset_tag", "asset-tag"),
        ])
        assert _same_row_writes(writes) == []

    def test_sets_are_per_table_never_across_tables(self, monkeypatch):
        writes = _run(monkeypatch, mappings=[
            _mapping("assets", "serial_number", "equipment-id"),
            _mapping("work_orders", "asset_tag", "equipment-id"),
        ])
        assert _same_row_writes(writes) == []

    def test_from_and_to_system_edges_accompany_each_node(self, monkeypatch):
        writes = _run(monkeypatch, mappings=[
            _mapping("assets", "serial_number", "equipment-id"),
            _mapping("assets", "asset_tag", "equipment-id"),
        ])
        sr = _same_row_writes(writes)
        assert len(sr) == 1
        jpid = sr[0][1]["jpid"]
        edges = [p for c, p in writes
                 if ("FROM_SYSTEM" in c or "TO_SYSTEM" in c)
                 and p.get("jpid") == jpid]
        assert len(edges) == 2

    def test_composite_unique_constraint_never_pairs_its_member_columns(self, monkeypatch):
        # S-A false-edge case: UNIQUE(line_id, asset_tag) makes the PAIR
        # jointly unique — neither column is 1:1 with the row on its own, so
        # composite membership must NOT count as per-column uniqueness
        # evidence. (Both columns are identifier-like and share no concept —
        # treating composite members as unique would have written a false
        # same_row edge here.)
        writes = _run(
            monkeypatch,
            mappings=[_mapping("work_orders", "line_id", "line-id")],
            unique_rows=[
                ("work_orders", "line_id", "uq_line_slot"),
                ("work_orders", "asset_tag", "uq_line_slot"),
            ],
        )
        assert _same_row_writes(writes) == []

    def test_composite_unique_does_not_poison_a_single_column_unique(self, monkeypatch):
        # A composite UNIQUE(line_id, asset_tag) alongside TWO single-column
        # UNIQUEs on the same table: only the single-column pair qualifies.
        writes = _run(
            monkeypatch,
            mappings=[_mapping("assets", "serial_number", "equipment-id")],
            unique_rows=[
                ("assets", "line_id", "uq_line_tag"),
                ("assets", "asset_tag", "uq_line_tag"),
                ("assets", "serial_number", "uq_serial_number"),
                ("assets", "asset_id", "uq_asset_id"),
            ],
        )
        sr = _same_row_writes(writes)
        assert sorted(p["jpid"] for _c, p in sr) == [
            "jp-samerow-cmms-assets-asset_id-serial_number"]

    def test_failed_unique_read_marks_the_provision_payload(self, monkeypatch):
        # S-D: the b5 UNIQUE info_schema read raising keeps best-effort
        # semantics (provision succeeds, concept-evidence pairs still write)
        # but the payload carries the additive degraded-read marker.
        writes, result = _run(
            monkeypatch,
            mappings=[
                _mapping("assets", "serial_number", "equipment-id"),
                _mapping("assets", "asset_tag", "equipment-id"),
            ],
            unique_read_raises=True,
            want_result=True,
        )
        assert result["status"] == "provisioned"
        assert result["same_row_unique_read_failed"] is True
        # Concept-evidence pairing is untouched by the failed UNIQUE read.
        assert [p["jpid"] for _c, p in _same_row_writes(writes)] == [
            "jp-samerow-cmms-assets-asset_tag-serial_number"]

    def test_unavailable_unique_read_marks_the_provision_payload(self, monkeypatch):
        # pg() never raises: a failed read comes back as an empty result
        # marked unavailable, which must not pass for "no UNIQUE constraints".
        writes, result = _run(
            monkeypatch,
            mappings=[_mapping("assets", "serial_number", "equipment-id"),
                      _mapping("assets", "asset_tag", "equipment-id")],
            unique_read_unavailable=True,
            want_result=True,
        )
        assert result["status"] == "provisioned"
        assert result["same_row_unique_read_failed"] is True
        assert [p["jpid"] for _c, p in _same_row_writes(writes)] == [
            "jp-samerow-cmms-assets-asset_tag-serial_number"]

    def test_successful_unique_read_omits_the_degraded_marker(self, monkeypatch):
        _writes, result = _run(
            monkeypatch,
            mappings=[_mapping("assets", "serial_number", "equipment-id")],
            unique_rows=[("assets", "serial_number", "uq_serial_number")],
            want_result=True,
        )
        assert result["status"] == "provisioned"
        assert "same_row_unique_read_failed" not in result
        assert "same_row_measure_read_failed" not in result


class TestSameConceptPairsAreMeasured:
    """A shared concept says two columns name the same KIND of thing, not the
    same thing on each row. A plant CMMS's ``work_orders.crew`` (a crew code)
    and ``reported_by`` (a person) can both map to ``operator``; writing them
    as a same-row set claimed a crew code identifies the reporter. A
    concept-only pair is written when its rows read 1:1."""

    def test_same_concept_pair_that_is_not_one_to_one_writes_nothing(self, monkeypatch):
        # The seeded work_orders shape: 3 crews, 4 reporters, 4 pairs.
        measured: list = []
        writes = _run(
            monkeypatch,
            mappings=[_mapping("work_orders", "crew", "operator"),
                      _mapping("work_orders", "reported_by", "operator")],
            pair_stats={("work_orders", "crew", "reported_by"): (3, 4, 4)},
            measured=measured,
        )
        assert _same_row_writes(writes) == []
        assert measured == [("work_orders", [("crew", "reported_by")])]

    def test_one_to_one_pair_is_written(self, monkeypatch):
        writes = _run(
            monkeypatch,
            mappings=[_mapping("assets", "asset_num", "equipment-id"),
                      _mapping("assets", "serial_number", "equipment-id")],
            pair_stats={("assets", "asset_num", "serial_number"): (6, 6, 6)},
        )
        assert [p["jpid"] for _c, p in _same_row_writes(writes)] == [
            "jp-samerow-cmms-assets-asset_num-serial_number"]

    @pytest.mark.parametrize("stats", [(6, 5, 6), (5, 6, 6), (6, 6, 7)])
    def test_either_direction_many_to_one_writes_nothing(self, monkeypatch, stats):
        writes = _run(
            monkeypatch,
            mappings=[_mapping("assets", "asset_num", "equipment-id"),
                      _mapping("assets", "serial_number", "equipment-id")],
            pair_stats={("assets", "asset_num", "serial_number"): stats},
        )
        assert _same_row_writes(writes) == []

    @pytest.mark.parametrize("pairs,written", [(0, False), (1, False), (2, False), (3, True)])
    def test_one_to_one_needs_the_minimum_distinct_pairs(self, monkeypatch, pairs, written):
        assert onboard.SAMEROW_MIN_DISTINCT_PAIRS == 3
        writes = _run(
            monkeypatch,
            mappings=[_mapping("assets", "asset_num", "equipment-id"),
                      _mapping("assets", "asset_tag", "equipment-id")],
            pair_stats={("assets", "asset_num", "asset_tag"): (pairs, pairs, pairs)},
        )
        assert bool(_same_row_writes(writes)) is written

    def test_one_read_per_table_for_all_its_pairs(self, monkeypatch):
        measured: list = []
        writes = _run(
            monkeypatch,
            mappings=[_mapping("assets", "asset_id", "equipment-id"),
                      _mapping("assets", "asset_tag", "equipment-id"),
                      _mapping("assets", "serial_number", "equipment-id")],
            measured=measured,
        )
        assert measured == [("assets", [("asset_id", "asset_tag"),
                                        ("asset_id", "serial_number"),
                                        ("asset_tag", "serial_number")])]
        assert len(_same_row_writes(writes)) == 3

    @pytest.mark.parametrize("status", ["failed", "too_large"])
    def test_unmeasured_table_writes_nothing_and_is_reported(self, monkeypatch, status):
        writes, result = _run(
            monkeypatch,
            mappings=[_mapping("assets", "asset_num", "equipment-id"),
                      _mapping("assets", "serial_number", "equipment-id")],
            table_status={"assets": status},
            want_result=True,
        )
        assert result["status"] == "provisioned"
        assert _same_row_writes(writes) == []
        assert result["same_row_not_measured"] == [
            {"table": "assets", "reason": status, "pairs": 1}]
        assert result.get("same_row_measure_read_failed", False) is (status == "failed")

    def test_measured_pairs_are_capped_per_provision(self, monkeypatch):
        # 20 columns sharing one concept = 190 pairs in one table, and a
        # second table after it. The cap holds across tables and each table
        # is still one read.
        cap = onboard.SAMEROW_MAX_MEASURED_PAIRS
        assert cap == 100
        cols = [f"ref_{i:02d}_id" for i in range(20)]
        measured: list = []
        writes, result = _run(
            monkeypatch,
            mappings=([_mapping("assets", c, "equipment-id") for c in cols]
                      + [_mapping("work_orders", "asset_id", "equipment-id"),
                         _mapping("work_orders", "asset_tag", "equipment-id")]),
            measured=measured,
            want_result=True,
        )
        assert [(tbl, len(pairs)) for tbl, pairs in measured] == [("assets", cap)]
        assert len(_same_row_writes(writes)) == cap
        assert result["same_row_not_measured"] == [
            {"table": "assets", "reason": "pair_cap", "pairs": 190 - cap},
            {"table": "work_orders", "reason": "pair_cap", "pairs": 1}]

    def test_both_unique_pair_is_not_measured(self, monkeypatch):
        # Two single-column UNIQUE columns are each 1:1 with the row, so the
        # DDL already proves the pair; no data read is spent on it.
        measured: list = []
        writes = _run(
            monkeypatch,
            mappings=[_mapping("assets", "serial_number", "equipment-id"),
                      _mapping("assets", "asset_tag", "equipment-id")],
            unique_rows=[("assets", "asset_tag", "uq_assets_asset_tag"),
                         ("assets", "serial_number", "uq_assets_serial_number")],
            pair_stats={("assets", "asset_tag", "serial_number"): (3, 4, 4)},
            measured=measured,
        )
        assert [p["jpid"] for _c, p in _same_row_writes(writes)] == [
            "jp-samerow-cmms-assets-asset_tag-serial_number"]
        assert measured == []

    def test_successful_measurement_omits_the_markers(self, monkeypatch):
        _writes, result = _run(
            monkeypatch,
            mappings=[_mapping("assets", "asset_num", "equipment-id"),
                      _mapping("assets", "serial_number", "equipment-id")],
            want_result=True,
        )
        assert "same_row_not_measured" not in result
        assert "same_row_measure_read_failed" not in result


class TestSameRowMeasureRead:
    """``_dp_samerow_measure_table``: one read of the whole table per call,
    the row limit bound, and a status for every way the read can fail."""

    def _measure(self, monkeypatch, answer, pairs=(("crew", "reported_by"),),
                 schema="cmms_plant"):
        seen: list = []

        def recording_pg(schema, sql, params=None):
            seen.append((schema, sql, params))
            return answer
        monkeypatch.setattr(onboard, "pg", recording_pg)
        ctx = SimpleNamespace(schema=schema)
        return onboard._dp_samerow_measure_table(ctx, "work_orders", list(pairs)), seen

    def test_reads_the_named_table_once_and_binds_the_limit(self, monkeypatch):
        pairs = [("asset_id", "asset_tag"), ("asset_id", "serial_number")]
        (status, stats), seen = self._measure(
            monkeypatch, {"rows": [[40, 40, 40, 40, 40, 39, 40]]}, pairs=pairs)
        assert status == "ok"
        assert stats == {("asset_id", "asset_tag"): (40, 40, 40),
                         ("asset_id", "serial_number"): (40, 39, 40)}
        ((schema, sql, params),) = seen
        assert schema == "cmms_plant"
        assert 'FROM "cmms_plant"."work_orders" LIMIT %s' in sql
        assert params == (onboard.SAMEROW_MEASURE_ROW_LIMIT + 1,)
        for col in ("asset_id", "asset_tag", "serial_number"):
            assert sql.count(f'CAST("{col}" AS TEXT)') == 1
        assert "WHERE" not in sql.split(") SELECT", 1)[0], "the read must not filter rows"

    def test_a_table_over_the_limit_is_too_large(self, monkeypatch):
        (status, stats), _seen = self._measure(
            monkeypatch, {"rows": [[onboard.SAMEROW_MEASURE_ROW_LIMIT + 1, 3, 3, 3]]})
        assert (status, stats) == ("too_large", {})

    def test_a_table_at_the_limit_is_measured(self, monkeypatch):
        (status, stats), _seen = self._measure(
            monkeypatch, {"rows": [[onboard.SAMEROW_MEASURE_ROW_LIMIT, 3, 4, 4]]})
        assert (status, stats) == ("ok", {("crew", "reported_by"): (3, 4, 4)})

    @pytest.mark.parametrize("answer", [
        {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
         "error_category": "OperationalError"},
        {"rows": []},
        {"rows": [[4, 4, 4]]},
    ])
    def test_an_unusable_answer_is_failed(self, monkeypatch, answer):
        (status, stats), _seen = self._measure(monkeypatch, answer)
        assert (status, stats) == ("failed", {})

    def test_a_raising_read_is_failed(self, monkeypatch):
        def raising_pg(*_a, **_k):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(onboard, "pg", raising_pg)
        ctx = SimpleNamespace(schema="cmms_plant")
        assert onboard._dp_samerow_measure_table(ctx, "work_orders", [("crew", "reported_by")]) == ("failed", {})

    def test_invalid_schema_is_never_interpolated(self, monkeypatch):
        (status, _stats), seen = self._measure(
            monkeypatch, {"rows": [[4, 4, 4, 4]]}, schema='x"; DROP TABLE y; --')
        assert status == "failed" and seen == []


def _reference_pairs(tbl, col_concept, tbl_unique, members):
    """The b5 pair split as the pre-cap code wrote it: every pair in order,
    both-unique pairs to the first list, concept-only pairs to the second."""
    unique_pairs, concept_pairs = [], []
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            same_concept = col_concept.get(a) and col_concept.get(a) == col_concept.get(b)
            both_unique = a in tbl_unique and b in tbl_unique
            if not (same_concept or both_unique):
                continue
            if any(',' in x or ' ' in x for x in (tbl, a, b)):
                continue
            if not all(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', x) for x in (tbl, a, b)):
                continue
            (unique_pairs if both_unique else concept_pairs).append((a, b))
    return unique_pairs, concept_pairs


class TestSameRowPairList:
    """``_dp_samerow_pairs`` lists only the concept-only pairs that fit in the
    room left under the cap and counts the rest without building them."""

    def test_matches_the_full_pair_list_for_every_room(self):
        rng = random.Random(1013)
        names = [f"c{i:02d}_id" for i in range(14)] + ["bad col", "9bad", "ok,no"]
        for _case in range(300):
            members = sorted(rng.sample(names, rng.randint(0, len(names))))
            col_concept = {c: rng.choice(["", "a", "b", "c"]) for c in members}
            tbl_unique = {c for c in members if rng.random() < 0.3}
            tbl = rng.choice(["assets", "assets", "bad table"])
            ref_unique, ref_concept = _reference_pairs(tbl, col_concept, tbl_unique, members)
            for room in (0, 1, 3, 7, 1000):
                got_unique, got_concept, over = onboard._dp_samerow_pairs(
                    tbl, col_concept, tbl_unique, members, room)
                assert got_unique == ref_unique
                assert got_concept == ref_concept[:room]
                assert over == len(ref_concept) - len(got_concept)

    def test_a_wide_table_lists_only_the_room(self):
        cols = [f"ref_{i:04d}_id" for i in range(1000)]
        unique, concept, over = onboard._dp_samerow_pairs(
            "assets", {c: "equipment-id" for c in cols}, set(), cols, 100)
        assert unique == [] and len(concept) == 100
        assert over == 1000 * 999 // 2 - 100
