# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for value-pair crosswalk mining (ADR-0058 — M1 miner core).

Pins the D2/D3/D4 contract:

  * ``value_pair_candidates`` — PURE alignment (affix-strip equality after a
    ladder rung + near-token pairs from ``crosswalk_candidates``), refusals
    (sequential-int shapes, digit-only pairs, identical values), the
    per-field-pair ``CROSSWALK_MAX_CANDIDATES`` cap, and determinism.
  * ``value_crosswalk_evidence`` — the COMPUTED "N of M sampled values"
    evidence string: counts present, at most FIVE examples, never LLM.
  * ``miner_field_pairs`` — the deterministic (sorted) per-run field-pair
    budget (``MINER_MAX_FIELD_PAIRS``, read at call time).
  * ``mine_value_crosswalks`` — filing through monkeypatched seams: caps
    enforced end-to-end, per-field sample caching, direction-canonical
    emission, structural dedup (re-runs yield ONLY dup_pending outcomes),
    never-raise isolation of a bad field pair, and the ADR-0052 usage bump.

No AWS / Neptune / LLM / network — graph reads, sampling, the proposal write,
and the usage ledger are monkeypatched module seams (same pattern as
``test_join_discovery_gates.py``). Synthetic generic values only — no dataset
literals (ADR-0044 neutrality ratchet runs over the module itself).
"""
from __future__ import annotations

import inspect

import pytest

import agents.join_discovery as jd


# =========================================================================== #
# Constants (ADR-0058 D4 cost bounds)
# =========================================================================== #
def test_miner_constants():
    assert jd.MINER_MAX_FIELD_PAIRS == 200
    assert jd.CROSSWALK_MAX_CANDIDATES == 5


# =========================================================================== #
# value_pair_candidates — PURE alignment, refusals, caps, determinism
# =========================================================================== #
class TestAffixAlignment:
    def test_strip_prefix_equality_aligns(self):
        a = ["WO-LINE-A", "WO-LINE-B", "WO-LINE-C"]
        b = ["LINE-A", "LINE-B", "LINE-C"]
        cands = jd.value_pair_candidates(a, b, "strip_prefix:WO-")
        assert [(c["from_value"], c["to_value"]) for c in cands] == [
            ("WO-LINE-A", "LINE-A"), ("WO-LINE-B", "LINE-B"),
            ("WO-LINE-C", "LINE-C")]
        assert all(c["method"] == "affix_strip:strip_prefix:WO-" for c in cands)
        assert all(c["support"] == 1.0 for c in cands)

    def test_strip_suffix_equality_aligns(self):
        cands = jd.value_pair_candidates(
            ["CELL-A_OLD", "CELL-B_OLD"], ["CELL-A", "CELL-B"],
            "strip_suffix:_OLD")
        assert [(c["from_value"], c["to_value"]) for c in cands] == [
            ("CELL-A_OLD", "CELL-A"), ("CELL-B_OLD", "CELL-B")]

    def test_identical_values_are_never_bridged(self):
        # An identity-shared value needs no crosswalk.
        assert jd.value_pair_candidates(
            ["LINE-A"], ["LINE-A"], "strip_prefix:WO-") == []

    def test_digit_only_pair_refused(self):
        # Neither side carries an alphabetic anchor → chance agreement.
        assert jd.value_pair_candidates(
            ["00123", "00456"], ["123", "456"], "strip_prefix:00") == []

    def test_one_alphabetic_anchor_suffices(self):
        # "WO-123" ↔ "123": the from side anchors the pair (the strip-prefix
        # bridge class the steward-earned set contains).
        cands = jd.value_pair_candidates(
            ["WO-123", "WO-456"], ["123", "456"], "strip_prefix:WO-")
        assert [(c["from_value"], c["to_value"]) for c in cands] == [
            ("WO-123", "123"), ("WO-456", "456")]

    def test_non_affix_transform_yields_no_affix_pairs(self):
        # identity / casefold / token_extract rungs have no affix to strip.
        for t in (jd.TRANSFORM_IDENTITY, jd.TRANSFORM_CASEFOLD,
                  jd.TRANSFORM_TOKEN_EXTRACT, None, ""):
            cands = jd.value_pair_candidates(["AAA-X"], ["BBB-Y"], t)
            assert all(c["method"] == "token_similarity" for c in cands)


class TestTokenSimilarityPath:
    def test_near_token_pairs_surface(self):
        cands = jd.value_pair_candidates(
            ["Milling Line 2"], ["Milling Cell 2"], None)
        assert len(cands) == 1
        c = cands[0]
        assert (c["from_value"], c["to_value"]) == ("Milling Line 2",
                                                    "Milling Cell 2")
        assert c["method"] == "token_similarity"
        assert c["support"] >= jd.CROSSWALK_MIN_SIMILARITY

    def test_positional_suffix_guard_carries_over(self):
        # ADR-0051 P6b rides in via crosswalk_candidates reuse.
        assert jd.value_pair_candidates(["AAA-2"], ["BBB-2"], None) == []

    def test_dissimilar_values_refused(self):
        assert jd.value_pair_candidates(["ALPHA"], ["OMEGA9X"], None) == []

    def test_affix_pairs_take_precedence_over_token_pairs(self):
        # A value already bridged by affix equality is not re-paired by
        # token similarity (one bridge per source/target value).
        a = ["WO-LINE-A", "WO-LINE-B"]
        b = ["LINE-A", "LINE-B"]
        cands = jd.value_pair_candidates(a, b, "strip_prefix:WO-")
        assert all(c["method"].startswith("affix_strip:") for c in cands)
        assert len(cands) == 2


class TestRefusalsAndCaps:
    def test_sequential_integer_shapes_refused(self):
        a = [str(i) for i in range(1, 9)]
        assert jd.value_pair_candidates(a, list(a), "strip_prefix:X") == []
        assert jd.value_pair_candidates(a, list(a), None) == []

    def test_empty_inputs_refused(self):
        assert jd.value_pair_candidates([], ["A"], None) == []
        assert jd.value_pair_candidates(["A"], [], None) == []
        assert jd.value_pair_candidates(None, None, None) == []

    def test_cap_at_crosswalk_max_candidates(self):
        n = jd.CROSSWALK_MAX_CANDIDATES + 3
        a = [f"WO-LINE-{chr(65 + i)}" for i in range(n)]
        b = [f"LINE-{chr(65 + i)}" for i in range(n)]
        cands = jd.value_pair_candidates(a, b, "strip_prefix:WO-")
        assert len(cands) == jd.CROSSWALK_MAX_CANDIDATES

    def test_explicit_max_candidates_honored(self):
        a = [f"WO-LINE-{chr(65 + i)}" for i in range(4)]
        b = [f"LINE-{chr(65 + i)}" for i in range(4)]
        cands = jd.value_pair_candidates(a, b, "strip_prefix:WO-",
                                         max_candidates=2)
        assert len(cands) == 2


class TestDeterminism:
    def test_same_input_same_output(self):
        a = ["WO-LINE-B", "WO-LINE-A", "Milling Line 2"]
        b = ["LINE-A", "LINE-B", "Milling Cell 2"]
        first = jd.value_pair_candidates(a, b, "strip_prefix:WO-")
        second = jd.value_pair_candidates(list(a), list(b), "strip_prefix:WO-")
        assert first == second

    def test_output_sorted_best_first(self):
        a = ["WO-LINE-B", "WO-LINE-A"]
        b = ["LINE-A", "LINE-B"]
        cands = jd.value_pair_candidates(a, b, "strip_prefix:WO-")
        keys = [(-c["support"], c["from_value"], c["to_value"]) for c in cands]
        assert keys == sorted(keys)


# =========================================================================== #
# value_crosswalk_evidence — the computed "N of M sampled values" string
# =========================================================================== #
class TestEvidenceString:
    def _pairs(self, n):
        return [{"from_value": f"WO-LINE-{i}", "to_value": f"LINE-{i}"}
                for i in range(n)]

    def test_counts_labels_method_and_date_present(self):
        s = jd.value_crosswalk_evidence(
            "work_orders.cell", "asset_hierarchy.line_name",
            "affix_strip:strip_prefix:WO-", 7, 42, self._pairs(2),
            "2026-08-07")
        assert s.startswith("cross-system mining 2026-08-07: ")
        assert "7 of 42 sampled work_orders.cell values" in s
        assert "align with asset_hierarchy.line_name" in s
        assert "via affix_strip:strip_prefix:WO-" in s
        assert "(e.g. WO-LINE-0 -> LINE-0; WO-LINE-1 -> LINE-1)" in s

    def test_at_most_five_examples(self):
        s = jd.value_crosswalk_evidence(
            "a.f", "b.g", "token_similarity", 9, 20, self._pairs(9), "2026-08-07")
        assert s.count("->") == 5

    def test_no_examples_no_tail(self):
        s = jd.value_crosswalk_evidence("a.f", "b.g", "m", 0, 10, [], "2026-08-07")
        assert "(e.g." not in s


# =========================================================================== #
# miner_field_pairs — the deterministic per-run field-pair budget
# =========================================================================== #
def _field(table, field, concept="", role="", data_type="varchar"):
    return {"table": table, "field": field, "concept": concept,
            "role": role, "data_type": data_type}


class TestFieldPairBudget:
    def test_sorted_cross_product_under_budget(self):
        fa = [_field("t2", "b_id"), _field("t1", "a_id")]
        fb = [_field("u1", "x_id")]
        pairs, truncated = jd.miner_field_pairs(fa, fb)
        assert truncated is False
        assert [(p[0]["field"], p[1]["field"]) for p in pairs] == [
            ("a_id", "x_id"), ("b_id", "x_id")]

    def test_truncation_is_deterministic_sorted_order(self):
        fa = [_field("t", f"f{i:02d}_id") for i in range(10)]
        fb = [_field("u", f"g{i:02d}_id") for i in range(10)]
        pairs, truncated = jd.miner_field_pairs(fa, fb, max_pairs=7)
        assert truncated is True
        assert len(pairs) == 7
        # Reversed input order yields the identical worklist.
        pairs2, _ = jd.miner_field_pairs(list(reversed(fa)),
                                         list(reversed(fb)), max_pairs=7)
        assert pairs == pairs2

    def test_default_budget_read_at_call_time(self, monkeypatch):
        monkeypatch.setattr(jd, "MINER_MAX_FIELD_PAIRS", 3)
        fa = [_field("t", f"f{i}_id") for i in range(4)]
        fb = [_field("u", "g_id")]
        pairs, truncated = jd.miner_field_pairs(fa, fb)
        assert truncated is True and len(pairs) == 3

    def test_empty_sides(self):
        assert jd.miner_field_pairs([], [_field("t", "a_id")]) == ([], False)
        assert jd.miner_field_pairs(None, None) == ([], False)


# =========================================================================== #
# mine_value_crosswalks — filing through monkeypatched seams
# =========================================================================== #
@pytest.fixture
def miner_wired(monkeypatch):
    """Wire the miner's seams: graph fields, samples, proposal store (with
    STRUCTURAL dedup mirroring log_proposed_mapping's lookup-before-write),
    and the usage-ledger bump."""
    env = {
        "fields": {},      # sid -> [field dicts]
        "samples": {},     # (sid, tbl, fld) -> [values]
        "proposals": [],   # every kwargs dict passed to the seam
        "outcomes": [],    # "new" | "dup_pending" per call
        "store": {},       # structural key -> mapping_id
        "bumps": [],       # refs passed to _bump_usage
        "sample_calls": [],
    }
    monkeypatch.setattr(jd, "_identifier_fields",
                        lambda sid: list(env["fields"].get(sid, [])))

    def _fake_sample(sid, tbl, fld):
        env["sample_calls"].append((sid, tbl, fld))
        return list(env["samples"].get((sid, tbl, fld), []))

    monkeypatch.setattr(jd, "_sample", _fake_sample)

    def _fake_log(**kwargs):
        env["proposals"].append(kwargs)
        key = tuple(kwargs.get(k, "") for k in (
            "proposed_type", "from_system", "from_field", "from_value",
            "to_system", "to_field", "to_value", "concept"))
        if key in env["store"]:
            env["outcomes"].append("dup_pending")
            return env["store"][key]
        mid = f"m{len(env['store']) + 1}"
        env["store"][key] = mid
        env["outcomes"].append("new")
        return mid

    monkeypatch.setattr(jd, "_log_proposed_mapping", _fake_log)
    monkeypatch.setattr(jd, "_bump_usage", lambda refs: env["bumps"].append(refs))
    return env


def _wire_affix_scenario(env, n=5):
    """sys-a work_orders.cell (WO-LINE-*) vs sys-b asset_hierarchy.line_name
    (LINE-*): the strip_prefix join clears the ladder → value bridges mined."""
    env["fields"]["sys-a"] = [_field("work_orders", "cell")]
    env["fields"]["sys-b"] = [_field("asset_hierarchy", "line_name")]
    env["samples"][("sys-a", "work_orders", "cell")] = [
        f"WO-LINE-{chr(65 + i)}" for i in range(n)]
    env["samples"][("sys-b", "asset_hierarchy", "line_name")] = [
        f"LINE-{chr(65 + i)}" for i in range(n)]


class TestMineValueCrosswalks:
    def test_joined_adjacent_pair_files_value_bridges(self, miner_wired):
        _wire_affix_scenario(miner_wired)
        result = jd.mine_value_crosswalks("sys-a", "sys-b")
        assert len(result["crosswalks"]) == jd.CROSSWALK_MAX_CANDIDATES
        assert result["field_pairs"] == 1
        assert result["truncated"] is False
        for p in miner_wired["proposals"]:
            assert p["proposed_type"] == "crosswalk"
            assert p["provenance"] == "adr-0058-machine-miner"
            assert p["method"] == "cross-system-miner"
            assert p["source"] == "agent"
            assert p["from_value"] and p["to_value"]

    def test_evidence_string_rides_query_context(self, miner_wired):
        _wire_affix_scenario(miner_wired, n=6)
        jd.mine_value_crosswalks("sys-a", "sys-b")
        ctx = miner_wired["proposals"][0]["query_context"]
        assert ctx.startswith("cross-system mining ")
        assert "5 of 6 sampled work_orders.cell values" in ctx
        assert "align with asset_hierarchy.line_name" in ctx
        assert "via affix_strip:strip_prefix:WO-" in ctx
        assert ctx.count("->") <= 5

    def test_shared_concept_disjoint_values_mines_token_pairs(self, miner_wired):
        env = miner_wired
        env["fields"]["sys-a"] = [_field("lines", "line_label", concept="line")]
        env["fields"]["sys-b"] = [_field("cells", "cell_label", concept="line")]
        env["samples"][("sys-a", "lines", "line_label")] = [
            "Milling Line 2", "Assembly Line 3"]
        env["samples"][("sys-b", "cells", "cell_label")] = [
            "Milling Cell 2", "Assembly Cell 3"]
        result = jd.mine_value_crosswalks("sys-a", "sys-b")
        assert len(result["crosswalks"]) == 2
        assert all(p["concept"] == "line" for p in env["proposals"])
        assert all("via token_similarity" in p["query_context"]
                   for p in env["proposals"])

    def test_no_join_no_shared_concept_files_nothing(self, miner_wired):
        env = miner_wired
        env["fields"]["sys-a"] = [_field("t", "a_code", concept="alpha")]
        env["fields"]["sys-b"] = [_field("u", "b_code", concept="beta")]
        env["samples"][("sys-a", "t", "a_code")] = ["Milling Line 2"]
        env["samples"][("sys-b", "u", "b_code")] = ["Milling Cell 2"]
        result = jd.mine_value_crosswalks("sys-a", "sys-b")
        assert result["crosswalks"] == []
        assert env["proposals"] == []

    def test_direction_canonical_emission(self, miner_wired):
        # zzz→aaa mining still emits aaa-side first: both sweep directions
        # produce the IDENTICAL proposal (one reviewable bridge).
        env = miner_wired
        env["fields"]["zzz-sys"] = [_field("work_orders", "cell")]
        env["fields"]["aaa-sys"] = [_field("asset_hierarchy", "line_name")]
        env["samples"][("zzz-sys", "work_orders", "cell")] = [
            f"WO-LINE-{chr(65 + i)}" for i in range(5)]
        env["samples"][("aaa-sys", "asset_hierarchy", "line_name")] = [
            f"LINE-{chr(65 + i)}" for i in range(5)]
        jd.mine_value_crosswalks("zzz-sys", "aaa-sys")
        for p in env["proposals"]:
            assert p["from_system"] == "aaa-sys"
            assert p["to_system"] == "zzz-sys"
            assert p["from_value"].startswith("LINE-")
            assert p["to_value"].startswith("WO-LINE-")

    def test_rerun_yields_only_dup_pending(self, miner_wired):
        _wire_affix_scenario(miner_wired)
        first = jd.mine_value_crosswalks("sys-a", "sys-b")
        n_new = len(miner_wired["outcomes"])
        assert set(miner_wired["outcomes"]) == {"new"}
        second = jd.mine_value_crosswalks("sys-a", "sys-b")
        rerun_outcomes = miner_wired["outcomes"][n_new:]
        assert set(rerun_outcomes) == {"dup_pending"}
        # Same ids come back; the store grew by zero rows.
        assert sorted(first["crosswalks"]) == sorted(second["crosswalks"])
        assert len(miner_wired["store"]) == n_new

    def test_field_pair_budget_enforced(self, miner_wired, monkeypatch):
        env = miner_wired
        monkeypatch.setattr(jd, "MINER_MAX_FIELD_PAIRS", 2)
        env["fields"]["sys-a"] = [_field("t", f"f{i}_id") for i in range(3)]
        env["fields"]["sys-b"] = [_field("u", f"g{i}_id") for i in range(2)]
        for i in range(3):
            env["samples"][("sys-a", "t", f"f{i}_id")] = ["Milling Line 2"]
        for i in range(2):
            env["samples"][("sys-b", "u", f"g{i}_id")] = ["Milling Cell 2"]
        result = jd.mine_value_crosswalks("sys-a", "sys-b")
        assert result["truncated"] is True
        assert result["field_pairs"] == 2

    def test_each_field_sampled_once(self, miner_wired):
        env = miner_wired
        env["fields"]["sys-a"] = [_field("t", "a_id"), _field("t", "b_id")]
        env["fields"]["sys-b"] = [_field("u", "x_id")]
        env["samples"][("sys-a", "t", "a_id")] = ["Milling Line 2"]
        env["samples"][("sys-a", "t", "b_id")] = ["Assembly Line 3"]
        env["samples"][("sys-b", "u", "x_id")] = ["Milling Cell 2"]
        jd.mine_value_crosswalks("sys-a", "sys-b")
        assert len(env["sample_calls"]) == len(set(env["sample_calls"])) == 3

    def test_bad_field_pair_is_isolated(self, miner_wired, monkeypatch):
        _wire_affix_scenario(miner_wired)
        miner_wired["fields"]["sys-a"].insert(
            0, _field("aaa", "broken_id"))  # sorts FIRST — mined before cell

        real_sample = jd._sample

        def _raising_sample(sid, tbl, fld):
            if fld == "broken_id":
                raise RuntimeError("simulated source outage")
            return real_sample(sid, tbl, fld)

        monkeypatch.setattr(jd, "_sample", _raising_sample)
        result = jd.mine_value_crosswalks("sys-a", "sys-b")
        # The healthy pair still mined despite the earlier failure.
        assert len(result["crosswalks"]) == jd.CROSSWALK_MAX_CANDIDATES

    def test_same_or_empty_system_refused(self, miner_wired):
        assert jd.mine_value_crosswalks("sys-a", "sys-a")["crosswalks"] == []
        assert jd.mine_value_crosswalks("", "sys-b")["crosswalks"] == []
        assert jd.mine_value_crosswalks("sys-a", "")["crosswalks"] == []

    def test_usage_bump_fires_only_when_rows_filed(self, miner_wired):
        env = miner_wired
        jd.mine_value_crosswalks("sys-a", "sys-b")  # nothing wired → no rows
        assert env["bumps"] == []
        _wire_affix_scenario(env)
        jd.mine_value_crosswalks("sys-a", "sys-b")
        assert env["bumps"] == [[("miner", "value_crosswalk")]]


# ---------------------------------------------------------------------------
# 50-line gate on every new function (project invariant)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("func", [
    jd._has_alpha,
    jd._affix_stripper,
    jd._affix_aligned_pairs,
    jd.value_pair_candidates,
    jd.value_crosswalk_evidence,
    jd.miner_field_pairs,
    jd._canonical_direction,
    jd._emit_value_crosswalk,
    jd._bump_usage,
    jd._make_cached_sampler,
    jd._mine_field_pair,
    jd.mine_value_crosswalks,
])
def test_new_functions_stay_under_the_50_line_gate(func):
    assert len(inspect.getsource(func).splitlines()) <= 50
