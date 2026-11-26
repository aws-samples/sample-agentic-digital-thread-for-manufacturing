# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the B5 steward digest (ADR-0052).

Covers the PURE ``learning_health.digest`` assembly and the
``GET /api/admin/learning/digest`` route:

  * top-3 worth_review ranks by the SHARED consequence scorer
    (``agents.proposal_scoring`` — stubbed onto the module here, so the
    parity assertion is BY CONSTRUCTION: digest and test consult the same
    module attributes, never a duplicated formula);
  * staging counts are reported separately, never folded into pending;
  * expired-unreviewed reuses the insight 30d-TTL semantics
    (``self_learning._is_stale_insight_pending``);
  * top artifacts capped at 5 with the legacy ``tool-text`` evidence fold;
  * a missing scorer (pre-2.2 tree) degrades worth_review ONLY — the rest
    of the digest stays live;
  * ADR-0063 D3 miner precision: the accept rate per method and overall,
    computed from persisted decisions inside a bounded window, with the
    rate ABSENT below the minimum sample, requeued rows excluded from BOTH
    numerator and denominator, and no gating path reading it;
  * route: pinned shape, 60 s cache (one build per window; only successful,
    NON-DEGRADED builds are cached — a raised error AND a zero shape
    returned by ``build_digest``'s internal swallow both skip the cache),
    backend errors degrade to zeros — NEVER a 500.

Auth note: ``/api/admin/*`` is gated by ``AuthMiddleware.ADMIN_PREFIXES``
before any handler runs (pinned in the middleware's own tests); the app
built here mounts the router bare, exercising handler behavior only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.learning_health as lh
import agents.learning_routes as lr
import agents.proposal_scoring as ps

_DIGEST_KEYS = {"worth_review", "pending_total", "expired_unreviewed",
                "top_artifacts", "quarantine", "staging", "guidance_budget"}
_ITEM_KEYS = {"mapping_id", "claim", "consequence", "provenance_tier"}


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _pending(mapping_id: str, *, score: float, tier: int = 2,
             ptype: str = "join_path", concept: str = "concept-a") -> dict:
    """A pending proposal row; the stub scorer reads ``_test_score``."""
    return {
        "mapping_id": mapping_id, "status": "pending",
        "proposed_type": ptype, "concept": concept,
        "from_system": "sys-a", "from_field": "t1.f1",
        "to_system": "sys-b", "to_field": "t2.f2",
        "discovered_at": _iso_days_ago(1), "_test_score": score,
        "_test_tier": tier,
    }


@pytest.fixture
def stub_scorer(monkeypatch):
    """Install the shared-scorer stub on ``proposal_scoring`` itself
    (``raising=False`` — works before AND after Task 2.2 lands). Digest and
    tests both resolve through the module, so ranking parity with whatever
    the shared module computes holds by construction."""
    monkeypatch.setattr(
        ps, "consequence",
        lambda row, concept_system_count: float(row["_test_score"])
        + float(concept_system_count),
        raising=False)
    monkeypatch.setattr(
        ps, "provenance_tier",
        lambda row: int(row.get("_test_tier", 2)), raising=False)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(lr.router)
    return app


@pytest.fixture
def client() -> TestClient:
    lr._reset_digest_cache()
    yield TestClient(_build_app())
    lr._reset_digest_cache()


# --------------------------------------------------------------------------- #
# Pure digest assembly
# --------------------------------------------------------------------------- #
def test_top3_by_consequence_shared_scorer(stub_scorer):
    """worth_review = top-3 pending ranked EXACTLY as the shared 2.2 scorer
    ranks the same fixture — expected order computed by calling the module's
    own ``consequence`` (parity, no duplicated formula)."""
    rows = [_pending(f"m{i}", score=s, tier=i % 3)
            for i, s in enumerate([1.5, 4.0, 0.2, 3.1])]
    expected = sorted(
        rows, key=lambda r: (-ps.consequence(r, 0), r["mapping_id"]))[:3]

    result = lh.digest(rows, usage={}, staging={"rows": 0,
                                                "distinct_subjects": 0})

    assert [i["mapping_id"] for i in result["worth_review"]] == [
        r["mapping_id"] for r in expected]
    for item, row in zip(result["worth_review"], expected):
        assert set(item) == _ITEM_KEYS
        assert item["consequence"] == pytest.approx(ps.consequence(row, 0))
        assert item["provenance_tier"] == ps.provenance_tier(row)
        assert "join_path" in item["claim"] and "sys-a" in item["claim"]


def test_top3_uses_concept_counts_seam(stub_scorer):
    """The concept→fan-out seam feeds the scorer's second argument."""
    rows = [_pending("m-low", score=1.0, concept="hot"),
            _pending("m-high", score=2.0, concept="cold")]

    result = lh.digest(rows, usage={},
                       staging={"rows": 0, "distinct_subjects": 0},
                       concept_counts={"hot": 5})

    # 1.0 + 5 fan-out outranks 2.0 + 0 through the SHARED scorer.
    assert [i["mapping_id"] for i in result["worth_review"]] == [
        "m-low", "m-high"]


def test_staging_counted_separately_from_pending(stub_scorer):
    """Staged rows appear ONLY in the staging sub-shape — never in
    pending_total or worth_review (ADR-0051 P5)."""
    rows = [
        _pending("m1", score=1.0),
        # Defense in depth: a staged row leaking past get_all_mappings'
        # filter must still be excluded from every pending number.
        {"mapping_id": "staged#leak", "status": "pending",
         "proposed_type": "join_path", "_test_score": 9.9},
    ]
    staging = {"rows": 3, "distinct_subjects": 2}

    result = lh.digest(rows, usage={}, staging=staging)

    assert result["staging"] == staging
    assert result["pending_total"] == 1
    assert [i["mapping_id"] for i in result["worth_review"]] == ["m1"]


def test_expired_unreviewed_counts_ttl_insights(stub_scorer):
    """Swept ``expired`` rows AND pending insights past the 30d TTL count;
    fresh pending insights do not (self_learning TTL semantics reused)."""
    rows = [
        {"mapping_id": "e1", "status": "expired", "proposed_type": "insight",
         "discovered_at": _iso_days_ago(45)},
        {"mapping_id": "stale", "status": "pending",
         "proposed_type": "insight", "discovered_at": _iso_days_ago(40)},
        {"mapping_id": "fresh", "status": "pending",
         "proposed_type": "insight", "discovered_at": _iso_days_ago(2)},
        _pending("m1", score=1.0),  # non-insight pending never counts
    ]

    result = lh.digest(rows, usage={}, staging={"rows": 0,
                                                "distinct_subjects": 0})

    assert result["expired_unreviewed"] == 2


def test_top_artifacts_capped_at_five_with_evidence_fold(stub_scorer):
    """Top artifacts sort by usage_30d desc, cap at 5; entries without
    ``evidence_classes`` (pre-B3 rows) fold under ``tool-text``."""
    usage = {f"a{i}": {"artifact_id": f"a{i}", "kind": "identity_rule",
                       "usage_30d": i} for i in range(1, 8)}
    usage["a7"]["evidence_classes"] = ["route-intersection"]

    result = lh.digest([], usage=usage, staging={"rows": 0,
                                                 "distinct_subjects": 0})

    top = result["top_artifacts"]
    assert [a["artifact_id"] for a in top] == ["a7", "a6", "a5", "a4", "a3"]
    assert top[0]["evidence_classes"] == ["route-intersection"]
    assert top[1]["evidence_classes"] == ["tool-text"]
    assert all(set(a) == {"artifact_id", "kind", "usage_30d",
                          "evidence_classes"} for a in top)


def test_missing_scorer_degrades_worth_review_only(monkeypatch):
    """Pre-2.2 tree (scorer not landed): worth_review degrades to [] while
    every other digest stat stays live — never an exception."""
    monkeypatch.delattr(ps, "consequence", raising=False)
    monkeypatch.delattr(ps, "provenance_tier", raising=False)
    rows = [_pending("m1", score=1.0),
            {"mapping_id": "q1", "status": "pending", "quarantined": "true",
             "proposed_type": "join_path", "_test_score": 0.1}]

    result = lh.digest(rows, usage={}, staging={"rows": 1,
                                                "distinct_subjects": 1})

    assert result["worth_review"] == []
    assert result["pending_total"] == 2
    assert result["quarantine"] == {"pending_quarantined": 1}
    assert result["staging"] == {"rows": 1, "distinct_subjects": 1}


def test_zero_digest_shape():
    assert set(lh.zero_digest()) == _DIGEST_KEYS
    assert lh.zero_digest()["worth_review"] == []


# --------------------------------------------------------------------------- #
# Wave-3 Task 3.1 — the pinned ``drift`` section + ``cost_trend`` honesty.
# --------------------------------------------------------------------------- #
def _drift_row(mid, days_old, *, reason="column_added", status="pending"):
    return {"mapping_id": mid, "status": status,
            "proposed_type": "schema_drift", "reason": reason,
            "discovered_at": _iso_days_ago(days_old)}


def test_drift_section_shape(stub_scorer):
    """Pinned shape: pending count, oldest_age_days from discovered_at,
    by_kind bucketed on the drift ``reason`` attr; non-drift and
    non-pending rows never count."""
    rows = [_drift_row("d1", 9.0), _drift_row("d2", 2.0,
                                              reason="column_removed"),
            _drift_row("d-acked", 30.0, status="accepted"),
            _pending("m1", score=1.0)]

    result = lh.digest(rows, usage={}, staging={"rows": 0,
                                                "distinct_subjects": 0})

    drift = result["drift"]
    assert set(drift) == {"pending", "oldest_age_days", "by_kind"}
    assert drift["pending"] == 2
    assert drift["oldest_age_days"] == pytest.approx(9.0, abs=0.1)
    assert drift["by_kind"] == {"column_added": 1, "column_removed": 1}


def test_drift_section_measured_zero_vs_zero_digest_absent(stub_scorer):
    """A successful build with no drift rows reports a MEASURED
    ``{"pending": 0}``; the degraded ``zero_digest`` shape carries NO drift
    key (absent = unavailable — the honesty distinction)."""
    result = lh.digest([_pending("m1", score=1.0)], usage={},
                       staging={"rows": 0, "distinct_subjects": 0})
    assert result["drift"] == {"pending": 0, "oldest_age_days": None,
                               "by_kind": {}}
    assert "drift" not in lh.zero_digest()
    assert "cost_trend" not in lh.zero_digest()


def test_cost_trend_absent_when_ledger_unavailable(stub_scorer):
    """``read_cost_trend`` folds backend failure AND no-measurements to
    ``[]`` — the digest must then carry NO cost_trend key, never an empty
    or zeroed section."""
    result = lh.digest([], usage={},
                       staging={"rows": 0, "distinct_subjects": 0},
                       cost_trend=[])
    assert "cost_trend" not in result
    assert "cost_trend" not in lh.digest(
        [], usage={}, staging={"rows": 0, "distinct_subjects": 0})


def test_cost_trend_section_shape_and_avg(stub_scorer):
    """Pinned section: days list with avg_total_tokens = (in+out)/turns,
    scope stamped orchestrator-model-only; 0-turn days skipped (a bump
    implies a turn — a 0-turn day is not a measurement)."""
    trend = [{"date": "2026-07-25", "turns": 2,
              "input_tokens": 10000, "output_tokens": 2000},
             {"date": "2026-07-26", "turns": 0,
              "input_tokens": 0, "output_tokens": 0}]

    result = lh.digest([], usage={},
                       staging={"rows": 0, "distinct_subjects": 0},
                       cost_trend=trend)

    section = result["cost_trend"]
    assert section["scope"] == "orchestrator-model-only"
    assert section["days"] == [{"date": "2026-07-25", "turns": 2,
                                "avg_total_tokens": 6000}]


def test_build_digest_feeds_cost_trend_from_ledger(stub_scorer, monkeypatch):
    """build_digest reads ``cost_ledger.read_cost_trend(7)`` — THE read
    path for the Wave-2 cost writer (ADR-0052 writer↔reader invariant)."""
    import agents.artifact_usage as artifact_usage
    import agents.cost_ledger as cost_ledger
    import agents.self_learning as self_learning

    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "get_all_query_guidance", lambda **_k: [])
    monkeypatch.setattr(artifact_usage, "read_artifact_usage",
                        lambda days=30: {})
    monkeypatch.setattr(lh, "staging_census",
                        lambda: {"rows": 0, "distinct_subjects": 0})
    monkeypatch.setattr(lh, "concept_system_counts", lambda ids: {})
    seen: list[int] = []

    def fake_trend(days=7):
        seen.append(days)
        return [{"date": "2026-07-25", "turns": 1,
                 "input_tokens": 100, "output_tokens": 50}]

    monkeypatch.setattr(cost_ledger, "read_cost_trend", fake_trend)

    result = lh.build_digest()

    assert seen == [7]
    assert result["cost_trend"]["days"][0]["avg_total_tokens"] == 150
    assert result["drift"] == {"pending": 0, "oldest_age_days": None,
                               "by_kind": {}}


# --------------------------------------------------------------------------- #
# build_digest ranks with the SAME concept fan-out inputs as the
# review list (the shared learning_health.concept_system_counts read).
# --------------------------------------------------------------------------- #
def test_build_digest_ranks_with_shared_concept_fanout(stub_scorer, monkeypatch):
    import agents.artifact_usage as artifact_usage
    import agents.self_learning as self_learning

    rows = [_pending("m-low", score=1.0, concept="hot"),
            _pending("m-high", score=2.0, concept="cold")]
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: rows)
    monkeypatch.setattr(self_learning, "get_all_query_guidance", lambda **_k: [])
    monkeypatch.setattr(artifact_usage, "read_artifact_usage",
                        lambda days=30: {})
    monkeypatch.setattr(lh, "staging_census",
                        lambda: {"rows": 0, "distinct_subjects": 0})
    seen: list[set] = []

    def fake_counts(ids, query=None):
        seen.append(set(ids))
        return {"hot": 5}

    monkeypatch.setattr(lh, "concept_system_counts", fake_counts)

    result = lh.build_digest()

    # ONE batched read over exactly the pending rows' concepts.
    assert seen == [{"hot", "cold"}]
    # Fan-out flows into the shared scorer: 1.0 + 5 outranks 2.0 + 0.
    assert [i["mapping_id"] for i in result["worth_review"]] == [
        "m-low", "m-high"]


def test_shared_concept_fanout_helper_is_batched_and_degrades(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_query(cypher, params=None):
        calls.append((cypher, params or {}))
        return [{"id": "hot", "systems": ["sys-a", "sys-b"]},
                {"id": "hot", "systems": ["sys-b", "sys-c"]}]

    counts = lh.concept_system_counts({"hot", "", None}, query=fake_query)
    assert counts == {"hot": 3}          # union-folded distinct systems
    assert len(calls) == 1               # ONE read for ALL ids — no N+1
    assert calls[0][1] == {"ids": ["hot"]}  # empties dropped
    assert lh.concept_system_counts(set(), query=fake_query) == {}
    assert lh.concept_system_counts(
        {"hot"}, query=lambda c, p=None: []) == {}


# --------------------------------------------------------------------------- #
# Route: GET /api/admin/learning/digest
# --------------------------------------------------------------------------- #
_DIGEST_FIXTURE = {
    "worth_review": [{"mapping_id": "m1", "claim": "join_path: a ↔ b",
                      "consequence": 3.4, "provenance_tier": 2}],
    "pending_total": 12,
    "expired_unreviewed": 3,
    "top_artifacts": [{"artifact_id": "jp-x", "kind": "join_path",
                       "usage_30d": 4, "evidence_classes": ["tool-text"]}],
    "quarantine": {"pending_quarantined": 4},
    "staging": {"rows": 3, "distinct_subjects": 2},
    "guidance_budget": {"available": True, "systems": 2,
                        "over_budget": True,
                        "over_by": 88, "over_budget_systems": [
                            {"system_id": "sys-a", "over_by": 88}]},
}


def test_digest_route_returns_pinned_shape(client, monkeypatch):
    monkeypatch.setattr(lh, "build_digest", lambda: dict(_DIGEST_FIXTURE))

    response = client.get("/api/admin/learning/digest")

    assert response.status_code == 200
    body = response.json()
    assert body == _DIGEST_FIXTURE
    assert set(body) == _DIGEST_KEYS


def test_digest_route_caches_for_60s(client, monkeypatch):
    """Second request inside the window serves the cache — ONE build."""
    calls = []

    def spy():
        calls.append(True)
        return dict(_DIGEST_FIXTURE)

    monkeypatch.setattr(lh, "build_digest", spy)

    assert client.get("/api/admin/learning/digest").status_code == 200
    assert client.get("/api/admin/learning/digest").status_code == 200
    assert calls == [True]


def test_digest_route_error_degrades_to_zeros_never_500(client, monkeypatch):
    def boom():
        raise RuntimeError("backend down")

    monkeypatch.setattr(lh, "build_digest", boom)

    response = client.get("/api/admin/learning/digest")

    assert response.status_code == 200
    assert response.json() == lh.zero_digest()


def test_digest_route_never_caches_degraded_shape(client, monkeypatch):
    """A failed build is NOT cached — the next request rebuilds."""
    outcomes = [RuntimeError("down"), dict(_DIGEST_FIXTURE)]

    def flaky():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(lh, "build_digest", flaky)

    assert client.get("/api/admin/learning/digest").json() == lh.zero_digest()
    assert client.get("/api/admin/learning/digest").json() == _DIGEST_FIXTURE


def test_digest_route_never_caches_zero_shape_returned_as_success(
        client, monkeypatch):
    """``build_digest``'s own internal swallow returns
    ``zero_digest()`` as a SUCCESS — the route must not pin that degraded
    shape for a 60 s window either. The next request rebuilds and the good
    shape IS cached (one further build, then served from cache)."""
    calls = []
    outcomes = [lh.zero_digest(), dict(_DIGEST_FIXTURE)]

    def internally_degraded_then_good():
        calls.append(True)
        return outcomes.pop(0)

    monkeypatch.setattr(lh, "build_digest", internally_degraded_then_good)

    assert client.get("/api/admin/learning/digest").json() == lh.zero_digest()
    assert client.get("/api/admin/learning/digest").json() == _DIGEST_FIXTURE
    # The good shape is cached — the third request triggers no third build.
    assert client.get("/api/admin/learning/digest").json() == _DIGEST_FIXTURE
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Stale-system partition: pending rows naming systems that are no longer
# onboarded are UNREVIEWABLE (the steward cannot accept a mapping into a system
# that does not exist). Observed live: demo-era rows from a prior data mode
# outlived their systems and one rendered ABOVE real candidates in the digest.
# Not deleted, not machine-rejected (ADR-0053) — counted separately.
# --------------------------------------------------------------------------- #
def _row(mid, frm, to, **kw):
    base = {"mapping_id": mid, "status": "pending", "from_system": frm,
            "to_system": to, "proposed_type": "value_mapping"}
    base.update(kw)
    return base


def test_stale_system_rows_are_partitioned_out():
    pending = [_row("m1", "sys-a", "sys-b"), _row("m2", "ghost", "sys-a"),
               _row("m3", "sys-a", "")]        # empty side never counts stale
    actionable, stale = lh._split_stale_system_rows(pending, {"sys-a", "sys-b"})
    assert [r["mapping_id"] for r in actionable] == ["m1", "m3"]
    assert stale == 1


def test_unknown_liveness_disables_filtering_rather_than_guessing():
    pending = [_row("m1", "ghost", "ghost2")]
    actionable, stale = lh._split_stale_system_rows(pending, None)
    assert actionable == pending and stale == 0


def test_digest_reports_stale_refs_only_when_liveness_known():
    rows = [_row("m1", "sys-a", "sys-b"), _row("m2", "ghost", "sys-a")]
    with_ids = lh.digest(rows, {}, {}, onboarded_ids={"sys-a", "sys-b"})
    assert with_ids["pending_total"] == 1
    assert with_ids["stale_system_refs"] == 1
    assert all("ghost" not in item["claim"] for item in with_ids["worth_review"])
    without = lh.digest(rows, {}, {})
    assert without["pending_total"] == 2
    assert "stale_system_refs" not in without   # key-absent = unknown


def test_onboarded_ids_helper_returns_none_not_empty_on_failure(monkeypatch):
    """An unreadable catalog must mean 'liveness unknown', never 'everything
    is stale' (an empty set would silently mean the latter)."""
    import agents.catalog as cat
    monkeypatch.setattr(cat.catalog, "get_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                        raising=False)
    assert lh._onboarded_system_ids() is None


# --------------------------------------------------------------------------- #
# ADR-0063 D3 — miner precision: COMPUTED from persisted steward decisions
# over a bounded window, honest absence below the minimum sample, requeued
# rows excluded from BOTH numerator and denominator, REPORT-ONLY (no gate).
# --------------------------------------------------------------------------- #
def _decided(mapping_id: str, status: str, *, method: str = "cross-system-miner",
             days_ago: float = 1.0, **kw) -> dict:
    """An exact ADR-0058 machine-miner artifact with a steward terminal stamp."""
    stamp = "accepted_at" if status == "accepted" else "rejected_at"
    actor_key = "accepted_by" if status == "accepted" else "rejected_by"
    row = {"mapping_id": mapping_id, "status": status, "method": method,
           "proposed_type": "crosswalk", "source": "agent",
           "provenance": "adr-0058-machine-miner", actor_key: "steward-1",
           "from_system": "sys-a", "to_system": "sys-b",
           "discovered_at": _iso_days_ago(days_ago + 1),
           stamp: _iso_days_ago(days_ago)}
    row.update(kw)
    return row


def test_precision_rate_computed_overall_and_per_method():
    """accepted / (accepted + rejected), per method AND overall — computed
    from the decisions alone (no inference, no LLM)."""
    rows = ([_decided(f"a{i}", "accepted") for i in range(3)]
            + [_decided(f"r{i}", "rejected") for i in range(9)]
            + [_decided(f"other{i}", "rejected", method="other-miner")
               for i in range(10)])

    result = lh.decision_precision(rows)

    assert result["decisions"] == 12
    assert result["accepted"] == 3
    assert result["rate"] == pytest.approx(3 / 12, abs=0.001)
    assert result["by_method"] == {
        "cross-system-miner": {
            "decisions": 12, "accepted": 3, "rate": round(3 / 12, 3)}}


def test_precision_excludes_requeued_from_numerator_and_denominator():
    """A requeue corrects the ACCEPT, not the proposal — a row carrying
    ``requeued_at`` counts in NEITHER the numerator nor the denominator
    (whether it sits back in pending or was later re-accepted)."""
    clean = [_decided(f"r{i}", "rejected") for i in range(10)]
    requeued_pending = {"mapping_id": "rq-pending", "status": "pending",
                        "method": "cross-system-miner",
                        "requeued_at": _iso_days_ago(1),
                        "review_reason": "requeued for steward review"}
    requeued_reaccepted = _decided("rq-again", "accepted",
                                   requeued_at=_iso_days_ago(2))

    result = lh.decision_precision(clean + [requeued_pending,
                                            requeued_reaccepted])

    assert result["decisions"] == 10          # the requeued accept is absent
    assert result["accepted"] == 0            # …from the numerator too
    assert result["rate"] == 0.0
    assert result["by_method"]["cross-system-miner"]["decisions"] == 10


def test_precision_rate_absent_below_minimum_but_count_kept():
    """Below the minimum decision count the RATE key is ABSENT while the
    count still shows — a small sample states its own smallness (ADR-0053).
    Applies per method as well as overall."""
    rows = [_decided("a1", "accepted"), _decided("r1", "rejected"),
            _decided("other", "accepted", method="other-miner")]

    result = lh.decision_precision(rows)

    assert result["decisions"] == 2
    assert "rate" not in result               # never 0.67, never a guess
    assert result["min_decisions"] == lh._PRECISION_MIN_DECISIONS
    assert result["by_method"]["cross-system-miner"] == {"decisions": 2,
                                                   "accepted": 1}
    assert all("rate" not in e for e in result["by_method"].values())


def test_precision_zero_decisions_is_honest_not_a_rate():
    """No decisions at all: a MEASURED count of 0, no rate, no method
    buckets — and the degraded digest omits the key entirely (absent =
    unavailable, distinct from measured-zero)."""
    result = lh.decision_precision([{"mapping_id": "p1", "status": "pending"}])

    assert result["decisions"] == 0 and result["accepted"] == 0
    assert "rate" not in result
    assert result["by_method"] == {}
    assert "precision" not in lh.zero_digest()


def test_precision_window_is_bounded_by_the_decision_stamp():
    """Only decisions stamped inside the window count; older ones drop out,
    and a decision with no parseable stamp is not a measurement."""
    inside = [_decided(f"in{i}", "accepted", days_ago=2.0) for i in range(6)]
    outside = [_decided(f"old{i}", "rejected", days_ago=40.0)
               for i in range(6)]
    unstamped = {"mapping_id": "no-stamp", "status": "rejected",
                 "method": "cross-system-miner"}

    wide = lh.decision_precision(inside + outside + [unstamped],
                                 window_days=60)
    narrow = lh.decision_precision(inside + outside + [unstamped],
                                   window_days=7)

    assert wide["decisions"] == 12 and wide["rate"] == 0.5
    assert narrow["decisions"] == 6           # the 40-day-old rows dropped
    assert "rate" not in narrow               # …taking it under the minimum
    # The default window is the bounded 30 d — the 40-day-old rows are out.
    assert lh._PRECISION_WINDOW_DAYS == 30
    assert lh.decision_precision(inside + outside)["decisions"] == 6


def test_precision_unstated_method_is_not_canonical_miner_evidence():
    rows = [_decided(f"r{i}", "rejected", method="") for i in range(10)]
    result = lh.decision_precision(rows)
    assert result["decisions"] == 0
    assert result["by_method"] == {}


def test_digest_carries_precision_without_disturbing_any_other_field():
    """REPORT-ONLY: adding decided rows adds the precision block and changes
    NOTHING else in the digest — pending/verdict-bearing fields are byte
    identical with and without them."""
    pending = [_row("m1", "sys-a", "sys-b")]
    decided = [_decided(f"r{i}", "rejected") for i in range(10)]

    without = lh.digest(pending, {}, {}, onboarded_ids={"sys-a", "sys-b"})
    with_decisions = lh.digest(pending + decided, {}, {},
                               onboarded_ids={"sys-a", "sys-b"})

    assert with_decisions["precision"]["rate"] == 0.0
    assert {k: v for k, v in with_decisions.items() if k != "precision"} == {
        k: v for k, v in without.items() if k != "precision"}
    assert without["precision"]["decisions"] == 0


def test_precision_is_never_read_by_a_gating_path():
    """The block is a signal for humans: no filing/accept/auto-accept path
    and no audit gate may consult it (ADR-0063 D3 report-only)."""
    from pathlib import Path
    repo = Path(lh.__file__).resolve().parents[2]
    gating = [repo / "agentic_digital_thread" / "agents" / "self_learning.py",
              repo / "scripts" / "audit_learning.py"]
    for path in gating:
        assert path.exists(), path
        assert "decision_precision" not in path.read_text(encoding="utf-8")
    # Nor does the auto-accept block reason (the single gate source of truth)
    # gain a precision clause.
    import agents.self_learning as sl
    reason = sl._auto_accept_block_reason(
        {"proposed_type": "value_mapping", "method": "cross-system-miner",
         "confidence": 0.95, "from_system": "sys-a", "to_system": "sys-b"},
        verify=False)
    assert "precision" not in str(reason or "").lower()


def test_precision_counts_only_exact_attributed_adr0058_machine_artifacts():
    exact = [_decided(f"exact-{i}", "accepted" if i < 4 else "rejected",
                      proposed_type="join_path" if i % 2 else "crosswalk")
             for i in range(10)]
    excluded = [
        _decided("human", "accepted", source="user"),
        _decided("guidance", "rejected", proposed_type="guidance_note"),
        _decided("schema", "accepted", proposed_type="schema_drift"),
        _decided("context", "rejected", provenance="context-placement"),
        _decided("wrong-method", "accepted", method="other-miner"),
        _decided("unattended-a", "accepted", accepted_by=""),
        _decided("unattended-r", "rejected", rejected_by=""),
        _decided("requeue", "accepted", requeued_at=_iso_days_ago(0.5)),
    ]

    result = lh.decision_precision(exact + excluded)

    assert result["decisions"] == 10
    assert result["accepted"] == 4
    assert result["rate"] == 0.4
    assert set(result["by_method"]) == {"cross-system-miner"}


def test_precision_legacy_missing_provenance_is_not_miner_evidence():
    legacy = _decided("legacy", "accepted")
    legacy.pop("provenance")
    assert lh.decision_precision([legacy])["decisions"] == 0


def test_guidance_budget_is_report_only_digest_annotation(stub_scorer):
    result = lh.digest(
        [], usage={}, staging={"rows": 0, "distinct_subjects": 0},
        guidance_budget=lh.guidance_budget_stats([
            {"system_id": "sys-a", "over_budget": True, "over_by": 144},
            {"system_id": "sys-b", "over_budget": False, "over_by": 0},
        ]))

    assert result["guidance_budget"] == {
        "available": True, "systems": 2, "over_budget": True, "over_by": 144,
        "over_budget_systems": [{"system_id": "sys-a", "over_by": 144}],
    }
    assert result["pending_total"] == 0  # annotation changes no digest gate/input
