# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the join-discovery quality gates (queue-quality over recall).

Covers the four gates added after live evidence showed degenerate joins entering
the review queue:

  1. ``JOIN_MIN_OVERLAP_COUNT`` raised 2 → 5 (a couple of shared values is
     coincidence, not a join key).
  2. ``is_measure_like`` blocklist — a measure-shaped name (``*_count``,
     ``*_quantity``, ``sample_size``, ``*_rate``, …) is NEVER a join endpoint,
     beating every positive signal including a stale graph ``role='identifier'``.
  3. ``passes_key_cardinality`` floor — post-transform key sets need >= 5 distinct
     values per side, and an all-short-numeric shared key set (the small-int-range
     signature the ``strip_prefix`` rung can manufacture) declines the ladder.
  4. Defensive emit gate in ``discover_pair`` — BOTH fields must read as
     identifier-like and non-measure at the ``join_path`` emission site.

Regression fixtures mirror the documented live degenerate strands (generic
field-name shapes, synthetic systems/values — no dataset literals):
``strip_prefix`` onto an ``operator_id`` from unrelated id/measure columns via a
tiny small-int key space; ``pass_count``→``reject_quantity``;
``sample_size``→``spec_id`` — while a legitimate shared-UUID ``work_order_id``
join stays alive.

No AWS / Neptune / LLM / network — graph reads, sampling, and the proposal write
are monkeypatched module seams (same pattern as ``test_join_discovery.py``).
"""
from __future__ import annotations

import pytest

import agents.join_discovery as jd


# =========================================================================== #
# Gate 1 — overlap threshold raised to 5
# =========================================================================== #
class TestOverlapThreshold:
    def test_constants(self):
        assert jd.JOIN_MIN_OVERLAP_COUNT == 5
        assert jd.JOIN_MIN_RATIO == 0.3
        assert jd.JOIN_MIN_KEY_CARDINALITY == 5

    def test_three_shared_values_no_longer_clear(self):
        # Cleared the old >= 2 threshold; must decline under >= 5.
        assert jd.discover_transform(["A", "B", "C"], ["A", "B", "C"]) is None

    def test_four_shared_values_decline(self):
        assert jd.discover_transform(["A", "B", "C", "D"],
                                     ["A", "B", "C", "D"]) is None

    def test_five_shared_distinct_non_numeric_values_join(self):
        r = jd.discover_transform(["K1", "K2", "K3", "K4", "K5"],
                                  ["K1", "K2", "K3", "K4", "K5"])
        assert r is not None
        assert r["transform"] == jd.TRANSFORM_IDENTITY
        assert r["overlap_count"] == 5


# =========================================================================== #
# Gate 2 — measure-like blocklist beats everything, including role='identifier'
# =========================================================================== #
class TestMeasureLike:
    @pytest.mark.parametrize("name", [
        "fail_count", "pass_count", "reject_quantity", "sample_size",
        "scrap_rate", "order_qty", "total_amount", "yield_pct", "defect_percent",
        "line_total", "running_sum", "avg_cycle",
    ])
    def test_measure_names_are_measure_like(self, name):
        assert jd.is_measure_like(name)

    @pytest.mark.parametrize("name", [
        "operator_id", "work_order_id", "site_code", "serial_no", "asset_tag",
    ])
    def test_identifier_names_are_not_measure_like(self, name):
        assert not jd.is_measure_like(name)

    def test_blocklist_beats_identifier_role(self):
        # Live evidence: MAPS_TO role rows leak measures — a *_count column is
        # never a join endpoint, even with an explicit role='identifier'.
        assert not jd.is_identifier_like("pass_count", role="identifier")
        assert not jd.is_identifier_like("fail_count", role="identifier",
                                         data_type="integer")
        assert not jd.is_identifier_like("reject_quantity", role="identifier")
        assert not jd.is_identifier_like("sample_size", role="identifier")

    def test_blocklist_beats_identifier_name_tokens(self):
        # 'no'/'num' tokens would qualify positively; the measure token wins.
        assert not jd.is_identifier_like("qty_no")

    def test_genuine_identifier_role_still_qualifies(self):
        assert jd.is_identifier_like("work_order_id", role="identifier")
        assert jd.is_identifier_like("anything", role="identifier")


# =========================================================================== #
# Gate 3 — post-transform key-cardinality floor
# =========================================================================== #
class TestKeyCardinality:
    def test_tiny_key_space_declines(self):
        assert not jd.passes_key_cardinality({"A", "B", "C"}, {"A", "B", "C"},
                                             ["A", "B", "C"])
        # Either side below the floor rejects.
        assert not jd.passes_key_cardinality(
            {"A", "B", "C", "D"}, {"A", "B", "C", "D", "E", "F"}, ["A", "B"])

    def test_all_short_numeric_shared_keys_decline(self):
        # The small-int-range signature (the strip_prefix strand).
        a = {"1", "2", "7", "9", "12"}
        assert not jd.passes_key_cardinality(a, set(a), ["1", "12", "2", "7", "9"])

    def test_longer_numeric_keys_pass(self):
        a = {"1001", "1002", "1003", "1004", "1005"}
        assert jd.passes_key_cardinality(a, set(a), sorted(a))

    def test_non_numeric_keys_pass(self):
        a = {"K1", "K2", "K3", "K4", "K5"}
        assert jd.passes_key_cardinality(a, set(a), sorted(a))

    def test_mixed_examples_pass(self):
        # A single non-short-numeric shared key breaks the degenerate signature.
        a = {"1", "2", "7", "9", "X12"}
        assert jd.passes_key_cardinality(a, set(a), ["1", "2", "7", "9", "X12"])

    def test_full_shared_set_is_judged_not_a_digit_first_sample(self):
        # REGRESSION (F2): the predicate judges the FULL shared key set. A
        # sorted-[:5] sample of this set is all digits ('1'..'9' sort before
        # 'LINE-A'), which wrongly declined a legitimate mixed key space.
        shared = {"1", "2", "3", "7", "9", "LINE-A"}
        assert jd.passes_key_cardinality(shared, set(shared), shared)
        # Sanity: the biased sample alone WOULD have tripped the signature.
        assert sorted(shared)[:5] == ["1", "2", "3", "7", "9"]

    def test_all_short_numeric_full_shared_set_still_declines(self):
        shared = {"1", "2", "3", "7", "9", "12"}
        assert not jd.passes_key_cardinality(shared, set(shared), shared)

    def test_mixed_shared_set_passes_at_the_ladder_level(self):
        # REGRESSION (F2): end-to-end through discover_transform — the mixed
        # key set must survive the identity rung (old code fed the digit-first
        # sorted(shared)[:5] sample into the cardinality check → None).
        vals = ["1", "2", "3", "7", "9", "LINE-A"]
        r = jd.discover_transform(list(vals), list(vals))
        assert r is not None
        assert r["transform"] == jd.TRANSFORM_IDENTITY
        assert r["overlap_count"] == 6

    def test_all_short_numeric_set_still_declines_at_the_ladder_level(self):
        vals = ["1", "2", "3", "7", "9", "12"]
        assert jd.discover_transform(list(vals), list(vals)) is None

    def test_floor_is_wired_into_the_ladder(self):
        # Even with an explicit low min_count, a cleared rung must still pass the
        # post-transform cardinality floor (3 distinct < 5) → None.
        assert jd.discover_transform(["A", "B", "C"], ["A", "B", "C"],
                                     min_count=2) is None

    def test_strip_prefix_onto_small_int_range_declines(self):
        # REGRESSION (live strand): a systematic prefix strip manufacturing
        # overlap on a tiny small-int key space — cleared-but-degenerate rung
        # declines the whole ladder rather than falling through.
        assert jd.discover_transform(
            ["OP-1", "OP-2", "OP-7", "OP-9", "OP-12"],
            ["1", "2", "7", "9", "12"]) is None

    def test_strip_prefix_onto_real_key_space_still_joins(self):
        r = jd.discover_transform(
            ["OP-1001", "OP-1002", "OP-1003", "OP-1004", "OP-1005"],
            ["1001", "1002", "1003", "1004", "1005"])
        assert r is not None
        assert r["transform"] == "strip_prefix:OP-"


# =========================================================================== #
# Gate 4 — defensive both-fields gate at the join_path emission site
# =========================================================================== #
_UUIDS = [
    "3f1a9c2e-0000-4a5b-8c6d-0000000000%02d" % i for i in range(1, 6)
]


@pytest.fixture
def emit_wired(monkeypatch):
    """Wire discover_pair with stale-role graph rows mirroring the live strands.

    ``_identifier_fields`` is monkeypatched DIRECTLY (bypassing its own
    is_identifier_like filter) to simulate graph MAPS_TO rows with stale
    role='identifier' on measure columns — the defensive emit gate is the last
    line of defense."""
    fields = {
        "sys-a": [
            {"table": "ops", "field": "operator_id", "concept": "",
             "role": "identifier", "data_type": "varchar"},
            {"table": "insp", "field": "pass_count", "concept": "",
             "role": "identifier", "data_type": "integer"},
            {"table": "insp", "field": "sample_size", "concept": "",
             "role": "identifier", "data_type": "integer"},
            {"table": "wo", "field": "work_order_id", "concept": "work-order",
             "role": "identifier", "data_type": "varchar"},
        ],
        "sys-b": [
            {"table": "asset", "field": "asset_id", "concept": "",
             "role": "identifier", "data_type": "varchar"},
            {"table": "bom", "field": "bom_id", "concept": "",
             "role": "identifier", "data_type": "varchar"},
            {"table": "spec", "field": "spec_id", "concept": "",
             "role": "identifier", "data_type": "varchar"},
            {"table": "insp", "field": "fail_count", "concept": "",
             "role": "identifier", "data_type": "integer"},
            {"table": "insp", "field": "reject_quantity", "concept": "",
             "role": "identifier", "data_type": "integer"},
            {"table": "wo", "field": "work_order_id", "concept": "work-order",
             "role": "identifier", "data_type": "varchar"},
        ],
    }
    small_ints = ["1", "2", "7", "9", "12"]
    counts = ["1001", "1002", "1003", "1004", "1005"]
    sizes = ["100", "200", "300", "400", "500"]
    samples = {
        # The strip_prefix:OP- strand: OP-<small int> vs bare small-int ids.
        ("sys-a", "ops", "operator_id"): [f"OP-{v}" for v in small_ints],
        ("sys-b", "asset", "asset_id"): list(small_ints),
        ("sys-b", "bom", "bom_id"): list(small_ints),
        ("sys-b", "spec", "spec_id"): sizes,   # also overlaps sample_size below
        ("sys-b", "insp", "fail_count"): list(small_ints),
        # pass_count → reject_quantity: overlap clears the ladder (4-digit keys)
        # so ONLY the emit gate can stop it.
        ("sys-a", "insp", "pass_count"): counts,
        ("sys-b", "insp", "reject_quantity"): counts,
        # sample_size → spec_id: ladder clears (3-digit keys) — emit gate stops it.
        ("sys-a", "insp", "sample_size"): sizes,
        # Legit shared-UUID work-order join must stay alive.
        ("sys-a", "wo", "work_order_id"): list(_UUIDS),
        ("sys-b", "wo", "work_order_id"): list(_UUIDS),
    }
    proposals: list[dict] = []
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: list(fields.get(sid, [])))
    monkeypatch.setattr(jd, "_timestamp_fields", lambda sid: [])
    monkeypatch.setattr(jd, "_sample",
                        lambda sid, tbl, fld: list(samples.get((sid, tbl, fld), [])))
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: False)

    def _fake_log(**kwargs):
        proposals.append(kwargs)
        return f"m{len(proposals)}"

    monkeypatch.setattr(jd, "_log_proposed_mapping", _fake_log)
    return proposals


def _joins(proposals):
    return [p for p in proposals if p.get("proposed_type") == "join_path"]


def test_stale_identifier_role_measures_emit_zero_joins(emit_wired):
    """REGRESSION: pass_count→reject_quantity and sample_size→spec_id — ladder
    clears, but the defensive emit gate blocks measure endpoints on either side."""
    jd.discover_pair("sys-a", "sys-b")
    for p in _joins(emit_wired):
        for field in (p["from_field"], p["to_field"]):
            assert not jd.is_measure_like(field.split(".")[-1]), \
                f"measure-like endpoint leaked into a join proposal: {field}"


def test_strip_prefix_small_int_strand_emits_zero_joins(emit_wired):
    """REGRESSION: no strip_prefix:* join from operator_id onto unrelated
    id/measure columns via the tiny small-int key space."""
    jd.discover_pair("sys-a", "sys-b")
    assert [p for p in _joins(emit_wired)
            if "operator_id" in p["from_field"]] == []


def test_legit_uuid_work_order_join_stays_alive(emit_wired):
    """The ONLY join out of this wiring is the legitimate shared-UUID
    work_order_id ↔ work_order_id identity join."""
    result = jd.discover_pair("sys-a", "sys-b")
    joins = _joins(emit_wired)
    assert len(joins) == 1
    assert len(result["joins"]) == 1
    p = joins[0]
    assert p["from_field"] == "wo.work_order_id"
    assert p["to_field"] == "wo.work_order_id"
    assert p["transform"] == jd.TRANSFORM_IDENTITY
    assert p["concept"] == "work-order"


def test_identifier_fields_filters_stale_measure_roles(monkeypatch):
    """Belt AND suspenders: the real _identifier_fields (graph read) now also
    drops role='identifier' rows on measure-shaped columns."""
    rows = [
        {"table": "insp", "field": "pass_count", "concept": None,
         "role": "identifier", "data_type": "integer"},
        {"table": "wo", "field": "work_order_id", "concept": "work-order",
         "role": "identifier", "data_type": "varchar"},
    ]
    monkeypatch.setattr(jd, "_neptune_query", lambda cypher, params=None: list(rows))
    out = jd._identifier_fields("sys-a")
    assert [f["field"] for f in out] == ["work_order_id"]


# =========================================================================== #
# ADR-0051 P6a — sequential-integer chance-overlap rejection
# =========================================================================== #
class TestSequentialIntegerOverlap:
    def test_constants(self):
        assert jd.SEQUENTIAL_INT_MIN_COUNT == 3
        assert jd.SEQUENTIAL_INT_MAX_START == 2
        assert jd.SEQUENTIAL_INT_MIN_DENSITY == 0.8

    def test_sequential_integer_overlap_rejected(self):
        # Named regression (audited defect shape): a spec_id column and a
        # work_order_id column that BOTH enumerate 1..8 share 100% of their
        # values — by construction, not identity. No join, ever.
        spec_ids = [str(i) for i in range(1, 9)]
        work_order_ids = [str(i) for i in range(1, 9)]
        assert jd.discover_transform(spec_ids, work_order_ids) is None

    def test_longer_one_to_n_range_rejected(self):
        # 1..250 defeats the old <=2-digit shared-key signature in
        # passes_key_cardinality; the 1..N shape check still rejects it.
        a = [str(i) for i in range(1, 251)]
        b = [str(i) for i in range(1, 251)]
        assert jd.discover_transform(a, b) is None

    def test_real_key_overlap_still_passes(self):
        # Regression guard: UUID-like key sets keep flowing.
        keys = [f"0000000{i}-1111-2222-3333-44445555666{i}" for i in range(1, 7)]
        r = jd.discover_transform(keys, keys)
        assert r is not None and r["transform"] == jd.TRANSFORM_IDENTITY

    def test_sparse_numeric_ids_are_not_sequential(self):
        # Sparse high-value numerics are real ids, not an enumeration.
        a = ["1001", "2450", "3999", "5120", "8765", "9314"]
        r = jd.discover_transform(a, list(a))
        assert r is not None

    def test_one_sided_enumeration_alone_does_not_reject(self):
        # Only when BOTH sides classify as 1..N does the guard fire — the
        # is_sequential_integer_set predicate is one-sided by design.
        assert jd.is_sequential_integer_set(["1", "2", "3", "4"]) is True
        assert jd.is_sequential_integer_set(["1001", "2450", "3999"]) is False
        assert jd.is_sequential_integer_set(["A1", "2", "3"]) is False


# =========================================================================== #
# ADR-0051 P6b — positional-name-suffix guard (crosswalk candidates)
# =========================================================================== #
class TestPositionalSuffixGuard:
    def test_positional_suffix_similarity_rejected(self):
        # AAA-2 vs BBB-2: the ONLY agreement is the trailing position token.
        assert jd.is_positional_suffix_only_match("AAA-2", "BBB-2") is True
        assert jd.crosswalk_candidates(["AAA-2"], ["BBB-2"]) == []
        # The DEFECT shape: a long shared numeric suffix drives the fuzzy
        # ratio over the 0.6 floor (ZZZ-1234 vs KKK-1234 scores 0.625) while
        # the stems share nothing — pre-guard this entered the queue.
        assert jd._pair_similarity("ZZZ-1234", "KKK-1234") >= jd.CROSSWALK_MIN_SIMILARITY
        assert jd.is_positional_suffix_only_match("ZZZ-1234", "KKK-1234") is True
        assert jd.crosswalk_candidates(["ZZZ-1234"], ["KKK-1234"]) == []

    def test_differing_suffixes_never_flagged(self):
        assert jd.is_positional_suffix_only_match("AAA-2", "BBB-3") is False

    def test_similar_stems_with_shared_suffix_survive(self):
        # The stems themselves resemble each other — the suffix is not the
        # sole signal, so the pair stays a legitimate candidate.
        assert jd.is_positional_suffix_only_match(
            "Milling Line 2", "Milling Cell 2") is False
        cands = jd.crosswalk_candidates(["Milling Line 2"], ["Milling Cell 2"])
        assert len(cands) == 1

    def test_no_suffix_pairs_unaffected(self):
        assert jd.is_positional_suffix_only_match("ALPHA", "ALPHB") is False


# =========================================================================== #
# ADR-0058 — the value-crosswalk MINER honors the same gates
# =========================================================================== #
class TestMinerGates:
    """The measure-token and identifier gates hold on the miner path too:
    ``mine_value_crosswalks`` re-applies the defensive emit gate per field pair
    (stale graph roles never mine), and the chance-overlap refusals
    (P6a sequential-int, digit-only anchors) carry into value alignment."""

    @pytest.fixture
    def wired(self, monkeypatch):
        env = {"fields": {}, "samples": {}, "proposals": []}
        monkeypatch.setattr(jd, "_identifier_fields",
                            lambda sid: list(env["fields"].get(sid, [])))
        monkeypatch.setattr(
            jd, "_sample",
            lambda sid, tbl, fld: list(env["samples"].get((sid, tbl, fld), [])))

        def _fake_log(**kwargs):
            env["proposals"].append(kwargs)
            return f"m{len(env['proposals'])}"

        monkeypatch.setattr(jd, "_log_proposed_mapping", _fake_log)
        monkeypatch.setattr(jd, "_bump_usage", lambda refs: None)
        return env

    def test_stale_identifier_role_measures_never_mine(self, wired):
        # Graph MAPS_TO rows with stale role='identifier' on measure columns —
        # near-token values would otherwise align; the emit gate must block.
        wired["fields"]["sys-a"] = [
            {"table": "insp", "field": "pass_count", "concept": "qc",
             "role": "identifier", "data_type": "varchar"}]
        wired["fields"]["sys-b"] = [
            {"table": "insp", "field": "reject_quantity", "concept": "qc",
             "role": "identifier", "data_type": "varchar"}]
        wired["samples"][("sys-a", "insp", "pass_count")] = ["Milling Line 2"]
        wired["samples"][("sys-b", "insp", "reject_quantity")] = ["Milling Cell 2"]
        result = jd.mine_value_crosswalks("sys-a", "sys-b")
        assert result["crosswalks"] == []
        assert wired["proposals"] == []

    def test_one_measure_endpoint_blocks_the_pair(self, wired):
        wired["fields"]["sys-a"] = [
            {"table": "lines", "field": "line_label", "concept": "line",
             "role": "identifier", "data_type": "varchar"}]
        wired["fields"]["sys-b"] = [
            {"table": "insp", "field": "sample_size", "concept": "line",
             "role": "identifier", "data_type": "varchar"}]
        wired["samples"][("sys-a", "lines", "line_label")] = ["Milling Line 2"]
        wired["samples"][("sys-b", "insp", "sample_size")] = ["Milling Cell 2"]
        assert jd.mine_value_crosswalks("sys-a", "sys-b")["crosswalks"] == []

    def test_sequential_int_samples_never_mine(self, wired):
        # P6a at the miner level: two 1..N enumerations under a shared concept
        # align by construction — refused before any emission.
        seq = [str(i) for i in range(1, 9)]
        wired["fields"]["sys-a"] = [
            {"table": "t", "field": "step_id", "concept": "step",
             "role": "identifier", "data_type": "varchar"}]
        wired["fields"]["sys-b"] = [
            {"table": "u", "field": "seq_id", "concept": "step",
             "role": "identifier", "data_type": "varchar"}]
        wired["samples"][("sys-a", "t", "step_id")] = list(seq)
        wired["samples"][("sys-b", "u", "seq_id")] = list(seq)
        assert jd.mine_value_crosswalks("sys-a", "sys-b")["crosswalks"] == []

    def test_legit_identifier_pair_still_mines(self, wired):
        # Sanity: the gates block degenerates, not the mechanism.
        wired["fields"]["sys-a"] = [
            {"table": "work_orders", "field": "cell", "concept": "",
             "role": "identifier", "data_type": "varchar"}]
        wired["fields"]["sys-b"] = [
            {"table": "asset_hierarchy", "field": "line_name", "concept": "",
             "role": "identifier", "data_type": "varchar"}]
        wired["samples"][("sys-a", "work_orders", "cell")] = [
            f"WO-LINE-{chr(65 + i)}" for i in range(5)]
        wired["samples"][("sys-b", "asset_hierarchy", "line_name")] = [
            f"LINE-{chr(65 + i)}" for i in range(5)]
        result = jd.mine_value_crosswalks("sys-a", "sys-b")
        assert len(result["crosswalks"]) == jd.CROSSWALK_MAX_CANDIDATES
        for p in wired["proposals"]:
            assert p["proposed_type"] == "crosswalk"
            for field in (p["from_field"], p["to_field"]):
                assert not jd.is_measure_like(field.split(".")[-1])
