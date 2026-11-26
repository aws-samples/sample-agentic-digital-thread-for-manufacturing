# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Containment probe for SPARSE foreign keys (join discovery).

Live evidence (2026-09-24): ERP ``lot_mes_tracking.work_order_id`` references
MES ``work_orders.work_order_id`` exactly, but two independent
``DISTINCT … LIMIT 200`` samples shared nothing, so no MES↔ERP join was ever
proposed and supplier-to-quality-failure traceability dead-ended. When the
ladder misses on a pair that plausibly names the same key, discovery now
probes each sample for membership on the other side.
"""
from __future__ import annotations

import uuid

import pytest

from agents import join_discovery as jd
from agents import resolve_evidence as re_


def _uuids(n):
    return [str(uuid.UUID(int=i + 1)) for i in range(n)]


# ── pure verdicts ────────────────────────────────────────────────────────────
def test_uuid_child_found_in_parent_passes_even_at_low_ratio():
    child = _uuids(200)                       # sampled across several sites
    found = child[:40]                        # this site's share exists
    v = jd.judge_containment(child, found)
    assert v and v["transform"] == jd.TRANSFORM_IDENTITY
    assert v["method"] == "containment_probe" and v["overlap_count"] == 40


def test_short_codes_keep_the_normal_ratio_floor():
    sample = [f"L{i}" for i in range(100)]
    assert jd.judge_containment(sample, sample[:10]) is None   # 0.1 < 0.3
    assert jd.judge_containment(sample, sample[:40]) is not None


def test_too_few_hits_and_sequential_integers_never_join():
    assert jd.judge_containment(_uuids(50), _uuids(4)) is None
    ints = [str(i) for i in range(1, 60)]
    assert jd.judge_containment(ints, ints) is None


def test_found_values_outside_the_sample_are_ignored():
    assert jd.judge_containment(_uuids(10), ["not-in-sample-xxxxxxxx"] * 9) is None


def test_eligibility_is_same_column_or_shared_concept():
    a = {"table": "work_orders", "field": "work_order_id", "concept": ""}
    b = {"table": "lot_mes_tracking", "field": "work_order_id", "concept": ""}
    c = {"table": "lots", "field": "batch_id", "concept": ""}
    assert jd.containment_probe_eligible(a, b)
    assert not jd.containment_probe_eligible(a, c)
    assert jd.containment_probe_eligible({**a, "concept": "wo"}, {**c, "concept": "wo"})


# ── discover_pair integration ────────────────────────────────────────────────
@pytest.fixture
def pair(monkeypatch):
    parent, child = _uuids(400), _uuids(400)[350:] + _uuids(600)[450:]
    fields = {
        "mes": [{"table": "work_orders", "field": "work_order_id", "concept": "", "role": "", "data_type": "uuid"}],
        "erp": [{"table": "lot_mes_tracking", "field": "work_order_id", "concept": "", "role": "", "data_type": "uuid"}],
    }
    samples = {("mes", "work_orders", "work_order_id"): parent[:200],   # arbitrary sample
               ("erp", "lot_mes_tracking", "work_order_id"): child}
    universe = {"mes": set(parent), "erp": set(child)}
    probes = []
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: fields[sid])
    monkeypatch.setattr(jd, "_timestamp_fields", lambda sid: [])
    monkeypatch.setattr(jd, "_sample", lambda sid, t, f: samples[(sid, t, f)])
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: False)

    def _probe(sid, t, f, values):
        probes.append(sid)
        return [v for v in values if v in universe[sid]]

    monkeypatch.setattr(jd, "_probe", _probe)
    emitted = []
    monkeypatch.setattr(jd, "_log_proposed_mapping", lambda **kw: emitted.append(kw) or "id-1")
    return emitted, probes


def test_sparse_fk_is_found_by_probe_when_samples_do_not_overlap(pair):
    emitted, probes = pair
    out = jd.discover_pair("mes", "erp")
    assert out["joins"] == ["id-1"]
    (kw,) = emitted
    assert (kw["from_field"], kw["to_field"]) == ("work_orders.work_order_id",
                                                  "lot_mes_tracking.work_order_id")
    assert kw["transform"] == jd.TRANSFORM_IDENTITY
    assert "containment-probe join" in kw["query_context"]
    assert probes and probes[0] == "mes"      # child sample probed in the parent


def test_unprobeable_source_falls_back_to_no_join(pair, monkeypatch):
    emitted, _ = pair
    monkeypatch.setattr(jd, "_probe", lambda *a: None)
    assert jd.discover_pair("mes", "erp")["joins"] == []
    assert emitted == []


def test_probe_failure_never_breaks_discovery(pair, monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(jd, "_probe", boom)
    assert jd.discover_pair("mes", "erp")["joins"] == []


# ── probe_field_values (SQL shape) ───────────────────────────────────────────
def test_probe_is_parameterized_and_bounded(monkeypatch):
    seen = {}

    def fake_pg(schema, sql, params=None):
        seen.update(schema=schema, sql=sql, params=params)
        return {"rows": [["a1"], [None]]}

    import agents.db as db
    monkeypatch.setattr(db, "pg", fake_pg)
    vals = [f"v{i}" for i in range(500)] + ["v1", " ", None]
    out = re_.probe_field_values("erp", "PostgreSQL", "erp_data",
                                 "lot_mes_tracking", "lot_mes_tracking.work_order_id", vals)
    assert out == ["a1"]
    assert "= ANY(%s)" in seen["sql"] and "v1" not in seen["sql"]
    (arr,) = seen["params"]
    assert len(arr) == re_.PROBE_MAX_VALUES and len(set(arr)) == len(arr)


def test_probe_declines_non_postgres_and_unavailable(monkeypatch):
    assert re_.probe_field_values("x", "Athena", "s", "t", "f", ["a"]) is None
    import agents.db as db
    monkeypatch.setattr(db, "pg", lambda *a, **k: {"rows": [], "unavailable": True})
    assert re_.probe_field_values("x", "PostgreSQL", "s", "t", "f", ["a"]) is None


def test_probe_rejects_unsafe_identifiers(monkeypatch):
    import agents.db as db
    monkeypatch.setattr(db, "pg", lambda *a, **k: pytest.fail("must not query"))
    assert re_.probe_field_values("x", "PostgreSQL", "s", "t; drop table x", "f", ["a"]) is None
