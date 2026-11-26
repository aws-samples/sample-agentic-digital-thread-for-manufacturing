# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the generic, order-independent cross-system join discovery.

No AWS / Neptune / LLM / network — the graph reads and value sampling are
monkeypatched module seams; proposals are captured by a fake ``log_proposed_mapping``.
GENERIC — synthetic systems / fields / values only, no dataset/system/model literals.
"""
from __future__ import annotations

import pytest

import agents.join_discovery as jd


# =========================================================================== #
# PURE: normalization transform ladder
# =========================================================================== #
class TestTransformLadder:
    def test_identity_wins_on_exact_overlap(self):
        r = jd.discover_transform(["S1", "S2", "S3", "S4", "S5", "S6"],
                                  ["S1", "S2", "S3", "S4", "S5", "S9"])
        assert r is not None
        assert r["transform"] == jd.TRANSFORM_IDENTITY
        assert r["overlap_count"] == 5

    def test_casefold_wins_when_only_case_differs(self):
        r = jd.discover_transform(["site1", "site2", "site3", "site4", "site5", "site6"],
                                  ["SITE1", "SITE2", "SITE3", "SITE4", "SITE5", "XXX9"])
        assert r is not None
        assert r["transform"] == jd.TRANSFORM_CASEFOLD
        assert r["overlap_count"] == 5

    def test_strip_prefix_wins_on_systematic_affix(self):
        r = jd.discover_transform(
            ["WO-1001", "WO-1002", "WO-1003", "WO-1004", "WO-1005"],
            ["1001", "1002", "1003", "1004", "1005"])
        assert r is not None
        assert r["transform"] == "strip_prefix:WO-"
        assert r["overlap_count"] == 5

    def test_strip_suffix_wins_on_systematic_affix(self):
        r = jd.discover_transform(
            ["120.a", "340.a", "560.a", "780.a", "910.a"],
            ["120", "340", "560", "780", "910"])
        assert r is not None
        assert r["transform"] == "strip_suffix:.a"
        assert r["overlap_count"] == 5

    def test_token_extract_wins_on_json_blob(self):
        a = ['[{"name":"X1","id":"z"}]', '[{"name":"X2","id":"y"}]',
             '[{"name":"X3","id":"x"}]', '[{"name":"X4","id":"w"}]',
             '[{"name":"X5","id":"v"}]']
        b = ["X1", "X2", "X3", "X4", "X5", "X9"]
        r = jd.discover_transform(a, b)
        assert r is not None
        assert r["transform"] == jd.TRANSFORM_TOKEN_EXTRACT
        assert r["overlap_count"] == 5

    def test_low_overlap_declines(self):
        # Only ONE shared value → below JOIN_MIN_OVERLAP_COUNT.
        assert jd.discover_transform(["A", "B", "C"], ["A", "X", "Y"]) is None

    def test_low_ratio_declines(self):
        # 5 shared values clears the count floor, but 5/20 = 0.25 < JOIN_MIN_RATIO.
        shared = ["K1", "K2", "K3", "K4", "K5"]
        a = shared + [f"AX{i}" for i in range(15)]
        b = shared + [f"BZ{i}" for i in range(15)]
        assert jd.discover_transform(a, b) is None

    def test_empty_inputs_decline(self):
        assert jd.discover_transform([], ["A", "B"]) is None
        assert jd.discover_transform(["A", "B"], []) is None


# =========================================================================== #
# PURE: time-key selection
# =========================================================================== #
class TestTimeKey:
    def test_window_boundary_pairs_yield_window_semantics(self):
        tk = jd.pick_time_key(["start_date", "note"], ["event_time"])
        assert tk == {"from_time_field": "start_date",
                      "to_time_field": "event_time", "semantics": "window"}

    def test_instant_only_yields_instant_semantics(self):
        tk = jd.pick_time_key(["updated_at"], ["logged_timestamp"])
        assert tk["semantics"] == "instant"

    def test_missing_side_yields_none(self):
        assert jd.pick_time_key([], ["event_time"]) is None
        assert jd.pick_time_key(["start_date"], []) is None

    def test_is_timestamp_type(self):
        assert jd.is_timestamp_type("timestamp")
        assert jd.is_timestamp_type("TIMESTAMP WITHOUT TIME ZONE")
        assert jd.is_timestamp_type("date")
        assert not jd.is_timestamp_type("varchar")


# =========================================================================== #
# PURE: identifier-like field selection
# =========================================================================== #
class TestIdentifierLike:
    def test_identifier_role_always_qualifies(self):
        assert jd.is_identifier_like("anything", role="identifier")

    def test_identifier_name_tokens_qualify(self):
        assert jd.is_identifier_like("work_order_id")
        assert jd.is_identifier_like("site_code")
        assert jd.is_identifier_like("cell")

    def test_free_text_type_excluded_without_identifier_role(self):
        assert not jd.is_identifier_like("notes", role="correlate", data_type="text")

    def test_plain_measure_column_excluded(self):
        assert not jd.is_identifier_like("temperature", role="measure", data_type="double")


# =========================================================================== #
# PURE: crosswalk candidate generation (name/token similarity)
# =========================================================================== #
class TestCrosswalkCandidates:
    def test_shared_tokens_produce_candidate(self):
        cands = jd.crosswalk_candidates(["Granulator_1", "Extruder_2"],
                                        ["granulator machine 1", "extruder unit"])
        assert cands
        top = cands[0]
        assert top["from_value"] in ("Granulator_1", "Extruder_2")
        assert top["similarity"] >= jd.CROSSWALK_MIN_SIMILARITY

    def test_disjoint_names_produce_nothing(self):
        assert jd.crosswalk_candidates(["QQQ"], ["ZZZ_totally_different_9999"]) == []

    def test_behavioral_seam_is_stub_returning_empty(self):
        # Documented follow-on seam — present, callable, never fabricates.
        assert jd.behavioral_crosswalk_candidates("a", "b") == []


# =========================================================================== #
# ORCHESTRATION: order-independence + threshold decline (mocked seams)
# =========================================================================== #
@pytest.fixture
def wired(monkeypatch):
    """Wire synthetic systems A and B with identifier fields + sampled values.

    Two systems share the ``site`` concept with identical values (a real join) and
    each has a ``machine`` field with DISJOINT values (no auto-join)."""
    fields = {
        "sys-a": [
            {"table": "wo", "field": "cell", "concept": "site", "role": "identifier", "data_type": "varchar"},
            {"table": "wo", "field": "machine_name", "concept": "machine", "role": "identifier", "data_type": "varchar"},
        ],
        "sys-b": [
            {"table": "asset", "field": "site_name", "concept": "site", "role": "identifier", "data_type": "varchar"},
            {"table": "asset", "field": "machine", "concept": "machine", "role": "identifier", "data_type": "varchar"},
        ],
    }
    samples = {
        ("sys-a", "wo", "cell"): ["SITE1", "SITE2", "SITE3", "SITE4", "SITE5"],
        ("sys-b", "asset", "site_name"): ["SITE1", "SITE2", "SITE3", "SITE4", "SITE5"],
        # DISJOINT machine vocabularies — no value overlap.
        ("sys-a", "wo", "machine_name"): ["SITE1-CS6900Z-02", "SITE1-Canon-09"],
        ("sys-b", "asset", "machine"): ["Diamant1HC1", "Dryer0"],
    }
    times = {"sys-a": ["start_date", "complete_date"], "sys-b": ["event_time"]}

    proposals: list[dict] = []

    monkeypatch.setattr(jd, "_onboarded_systems", lambda: ["sys-a", "sys-b"])
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: list(fields.get(sid, [])))
    monkeypatch.setattr(jd, "_timestamp_fields", lambda sid: list(times.get(sid, [])))
    monkeypatch.setattr(jd, "_sample", lambda sid, tbl, fld: list(samples.get((sid, tbl, fld), [])))
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: False)

    def _fake_log(**kwargs):
        proposals.append(kwargs)
        return f"m{len(proposals)}"

    monkeypatch.setattr(jd, "_log_proposed_mapping", _fake_log)
    return proposals


def _join_proposals(proposals):
    return [p for p in proposals if p.get("proposed_type") == "join_path"]


def _site_join(proposals):
    """A join between the two site fields (either direction)."""
    for p in _join_proposals(proposals):
        fields = {p["from_field"], p["to_field"]}
        if fields == {"wo.cell", "asset.site_name"}:
            return p
    return None


def test_order_independence_a_then_b(wired):
    jd.discover_joins_for_system("sys-a")
    assert _site_join(wired) is not None


def test_order_independence_b_then_a(wired):
    jd.discover_joins_for_system("sys-b")
    assert _site_join(wired) is not None


def test_sweep_finds_join_for_preexisting_pair(wired):
    # Neither system "just onboarded" — the re-runnable sweep still finds it.
    result = jd.discover_all_joins()
    assert result["joins"]
    assert _site_join(wired) is not None


def test_site_join_carries_identity_transform_and_time_key(wired):
    jd.discover_joins_for_system("sys-a")
    p = _site_join(wired)
    assert p is not None
    assert p["transform"] == jd.TRANSFORM_IDENTITY
    assert p["concept"] == "site"
    # Both systems have timestamp fields → a time_key is attached.
    assert p["time_key"] is not None
    assert p["time_key"]["semantics"] == "window"  # start/complete boundary present


def test_disjoint_machines_do_not_auto_join(wired):
    jd.discover_joins_for_system("sys-a")
    for p in _join_proposals(wired):
        assert {p["from_field"], p["to_field"]} != {"wo.machine_name", "asset.machine"}


def test_disjoint_machines_yield_crosswalk_only_under_joined_parent(monkeypatch, wired):
    # Now assert the systems ARE already joined (a parent site JoinPath exists).
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: True)
    # Give the machine names SHARED tokens so a name-similarity candidate exists.
    monkeypatch.setattr(jd, "_sample", lambda sid, tbl, fld: {
        ("sys-a", "wo", "cell"): ["SITE1", "SITE2", "SITE3", "SITE4", "SITE5"],
        ("sys-b", "asset", "site_name"): ["SITE1", "SITE2", "SITE3", "SITE4", "SITE5"],
        ("sys-a", "wo", "machine_name"): ["Press Line 1", "Binder 2"],
        ("sys-b", "asset", "machine"): ["press line one", "binder two"],
    }.get((sid, tbl, fld), []))

    result = jd.discover_pair("sys-a", "sys-b")
    crosswalks = [p for p in wired if p.get("proposed_type") == "crosswalk"]
    assert crosswalks, "expected crosswalk candidates for disjoint machines under joined parent"
    assert result["crosswalks"]
    # A crosswalk carries a value↔value candidate pair, NOT a bare field join.
    cw = crosswalks[0]
    assert cw["from_value"] and cw["to_value"]
    assert cw["concept"] == "machine"


def test_no_crosswalk_without_joined_parent(wired):
    # _has_established_join is False (default fixture) → disjoint machines yield
    # neither a join NOR a crosswalk (nothing fabricated).
    jd.discover_pair("sys-a", "sys-b")
    assert [p for p in wired if p.get("proposed_type") == "crosswalk"] == []
