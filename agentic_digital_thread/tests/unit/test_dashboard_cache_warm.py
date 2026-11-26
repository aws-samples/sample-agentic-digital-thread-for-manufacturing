# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for background WARMING of the S5.4 dashboard card cache and
the invalidate hooks (perf: make ``/api/dashboard`` fast even cold/idle).

No AWS, no graph, no DB, no LLM — runs in the default (unmarked) offline lane,
mirroring ``tests/unit/test_dashboard_cards.py``. The dashboard imports the
resolver as a *module* and holds one process-wide ``_CARDS_CACHE``; the autouse
``_reset_dashboard_cards_cache`` fixture (``tests/unit/conftest.py``) invalidates
it around every test, so each case starts from a clean, unwarmed cache.

Covered:
  • ``refresh_cards_cache`` force-rebuilds + returns; fail-soft leaves prior cache.
  • ``update_cards_cache`` patches BY card_id, no-ops when unwarmed / disabled.
  • ``_cached_cards`` serves slightly-stale when a background warmer is active
    (CARD_LIVE) instead of blocking a cold resolve; flag-off is byte-identical
    inline-on-expiry; a cold start (nothing cached) still resolves inline.
  • The UNIFIED single-resolve path: ``rederive._publish_batch`` warms the cache
    from the SAME per-card resolve that drives the SSE ripple — no second full
    ``_build_cards`` scan (no doubling of backend queries).
  • The mapping-correction route invalidates the cache so a correction reflects
    on the next ``/api/dashboard``.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import Mock

import agents.dashboard as dashboard
import agents.metric_resolver as mr
import agents.rederive as rederive


def _expire(cards: list[dict]) -> None:
    """Seed the cache with a PRESENT-but-expired entry (ts far in the past)."""
    dashboard._CARDS_CACHE["cards"] = cards
    dashboard._CARDS_CACHE["ts"] = time.monotonic() - 100_000


# --- refresh_cards_cache -----------------------------------------------------

def test_refresh_cards_cache_populates_and_returns(monkeypatch):
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "9999")
    dashboard.invalidate_cards_cache()
    sentinel = [{"card_id": "a"}, {"card_id": "b"}]
    monkeypatch.setattr(dashboard, "_build_cards", lambda: sentinel)

    out = dashboard.refresh_cards_cache()

    assert out is sentinel
    # Cache is now warm — a following read serves it WITHOUT rebuilding.
    monkeypatch.setattr(
        dashboard, "_build_cards",
        Mock(side_effect=AssertionError("must not rebuild a warm cache")),
    )
    assert dashboard._cached_cards() is sentinel


def test_refresh_cards_cache_failsoft_keeps_prior_cache(monkeypatch):
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "9999")
    dashboard.invalidate_cards_cache()
    good = [{"card_id": "a"}]
    monkeypatch.setattr(dashboard, "_build_cards", lambda: good)
    dashboard.refresh_cards_cache()

    # A failing rebuild must NOT raise and must NOT blank the good cache.
    monkeypatch.setattr(
        dashboard, "_build_cards", Mock(side_effect=RuntimeError("boom")))
    out = dashboard.refresh_cards_cache()

    assert out == good
    assert dashboard._CARDS_CACHE["cards"] == good


# --- update_cards_cache (the single-resolve patch seam) ----------------------

def test_update_cards_cache_patches_by_id(monkeypatch):
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "9999")
    dashboard.invalidate_cards_cache()
    monkeypatch.setattr(
        dashboard, "_build_cards",
        lambda: [{"card_id": "a", "v": 1}, {"card_id": "b", "v": 1}],
    )
    dashboard.refresh_cards_cache()

    # Patch 'a' with a fresh value; an unknown id must NOT be added (scalar-only cache).
    dashboard.update_cards_cache([{"card_id": "a", "v": 99}, {"card_id": "zzz", "v": 5}])

    by = {c["card_id"]: c for c in dashboard._CARDS_CACHE["cards"]}
    assert by["a"]["v"] == 99          # patched from the fresh resolve
    assert by["b"]["v"] == 1           # sibling untouched
    assert "zzz" not in by             # unknown id not grown into the cache


def test_update_cards_cache_noop_when_unwarmed(monkeypatch):
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "9999")
    dashboard.invalidate_cards_cache()          # cards == None (unwarmed)

    dashboard.update_cards_cache([{"card_id": "a"}])

    assert dashboard._CARDS_CACHE["cards"] is None   # nothing to patch onto


def test_update_cards_cache_noop_when_caching_disabled(monkeypatch):
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "0")   # caching opt-out
    dashboard.invalidate_cards_cache()
    dashboard._CARDS_CACHE["cards"] = [{"card_id": "a", "v": 1}]

    dashboard.update_cards_cache([{"card_id": "a", "v": 2}])

    assert dashboard._CARDS_CACHE["cards"][0]["v"] == 1   # untouched when disabled


# --- _cached_cards serving policy (cold vs warm vs serve-stale) --------------

def test_cached_cards_serves_stale_when_warming(monkeypatch):
    # CARD_LIVE on ⇒ a background warmer keeps the cache fresh, so an EXPIRED but
    # present entry is served as-is rather than blocking on a cold resolve.
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "90")
    monkeypatch.setenv("CARD_LIVE", "1")
    dashboard.invalidate_cards_cache()
    stale = [{"card_id": "a"}]
    _expire(stale)
    build = Mock(side_effect=AssertionError("must not cold-resolve while warming"))
    monkeypatch.setattr(dashboard, "_build_cards", build)

    assert dashboard._cached_cards() is stale
    build.assert_not_called()


def test_cached_cards_flag_off_reresolves_on_expiry(monkeypatch):
    # CARD_LIVE off ⇒ byte-identical to the original S5.4 TTL behavior: an expired
    # entry re-resolves inline (no serve-stale).
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "90")
    monkeypatch.delenv("CARD_LIVE", raising=False)
    dashboard.invalidate_cards_cache()
    _expire([{"card_id": "old"}])
    fresh = [{"card_id": "new"}]
    monkeypatch.setattr(dashboard, "_build_cards", lambda: fresh)

    assert dashboard._cached_cards() is fresh


def test_cached_cards_reresolves_on_expiry_when_paused(monkeypatch):
    # CARD_LIVE on BUT live updates PAUSED ⇒ the warming poll no-ops, so a
    # present-but-expired cache must NOT be served stale forever — an on-demand
    # read falls through to a fresh inline resolve (regression guard: a bad
    # startup-warmed value could otherwise be served indefinitely while paused).
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "90")
    monkeypatch.setenv("CARD_LIVE", "1")
    dashboard.invalidate_cards_cache()
    _expire([{"card_id": "stale"}])
    fresh = [{"card_id": "fresh"}]
    monkeypatch.setattr(dashboard, "_build_cards", lambda: fresh)

    from agents import live_settings
    live_settings.set_live_updates(False)     # pause → warmer not actually running
    try:
        assert dashboard._cached_cards() is fresh   # inline resolve, not stale
    finally:
        live_settings.set_live_updates(None)  # clear override (isolation)


def test_cached_cards_cold_start_resolves_inline_even_when_warming(monkeypatch):
    # Only the very first cold start (nothing cached yet) resolves inline, even
    # with a background warmer active.
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "90")
    monkeypatch.setenv("CARD_LIVE", "1")
    dashboard.invalidate_cards_cache()          # cards == None
    fresh = [{"card_id": "x"}]
    monkeypatch.setattr(dashboard, "_build_cards", lambda: fresh)

    assert dashboard._cached_cards() is fresh
    assert dashboard._CARDS_CACHE["cards"] is fresh   # populated for next time


# --- UNIFIED single-resolve: the poll/rederive warms the cache ----------------

def test_publish_batch_warms_cache_single_resolve(monkeypatch):
    """The live poll enqueues covered cards → ``rederive._publish_batch`` resolves
    each ONCE (driving the SSE ripple) and feeds the SAME result to
    ``dashboard.update_cards_cache`` — so the /api/dashboard cache is warmed with
    NO second full ``_build_cards`` scan (no doubling of backend queries)."""
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "9999")
    dashboard.invalidate_cards_cache()
    monkeypatch.setattr(
        dashboard, "_build_cards",
        lambda: [{"card_id": "oee", "v": 1}, {"card_id": "spare-parts-on-hand", "v": 1}],
    )
    dashboard.refresh_cards_cache()   # warm with two cards

    # Counting per-card resolver == the SAME resolve that drives the SSE ripple.
    calls = {"n": 0}

    def _resolve(card_id):
        calls["n"] += 1
        return {"card_id": card_id, "v": 42}

    monkeypatch.setattr(rederive, "_resolve_card", _resolve)
    # A second FULL rebuild would be the double-resolve regression — forbid it.
    monkeypatch.setattr(
        dashboard, "_build_cards",
        Mock(side_effect=AssertionError("no second full resolve (unified)")),
    )
    published: list[dict] = []
    monkeypatch.setattr(rederive.card_events, "publish", lambda ev: published.append(ev))

    # Direct-cards item shape: (concepts, {card_id: None}, audit).
    rederive._publish_batch(set(), {"oee": None}, {})

    # (a) SSE ripple published once with the freshly-resolved card.
    assert published and published[-1]["status"] == "updated"
    assert [c["card_id"] for c in published[-1]["cards"]] == ["oee"]
    # (b) Cache warmed from the SAME resolve — oee patched, sibling untouched.
    by = {c["card_id"]: c for c in dashboard._CARDS_CACHE["cards"]}
    assert by["oee"]["v"] == 42
    assert by["spare-parts-on-hand"]["v"] == 1
    # (c) Single resolve: exactly one per-card resolve, zero full rebuilds.
    assert calls["n"] == 1


def test_publish_batch_cache_warm_is_failsoft(monkeypatch):
    """A cache-warm error inside the worker batch is swallowed — the SSE ripple
    still publishes (the warm is best-effort, never fatal to the worker)."""
    monkeypatch.setattr(rederive, "_resolve_card", lambda cid: {"card_id": cid})
    monkeypatch.setattr(
        dashboard, "update_cards_cache", Mock(side_effect=RuntimeError("boom")))
    published: list[dict] = []
    monkeypatch.setattr(rederive.card_events, "publish", lambda ev: published.append(ev))

    rederive._publish_batch(set(), {"oee": None}, {})   # must not raise

    assert published and published[-1]["status"] == "updated"


# --- invalidate hook: the mapping-correction route ---------------------------

def test_correct_mapping_route_invalidates_cache(monkeypatch):
    """A ``POST /api/metrics/correct-mapping`` drops the card cache so the
    correction reflects on the NEXT /api/dashboard (independent of the SSE flag)."""
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "9999")
    dashboard.invalidate_cards_cache()
    monkeypatch.setattr(dashboard, "_build_cards", lambda: [{"card_id": "oee"}])
    dashboard.refresh_cards_cache()
    assert dashboard._CARDS_CACHE["cards"] is not None   # warm before the correction

    # Stub the write + ripple so the route touches no AWS/graph; flag off so the
    # invalidate is exercised on its own (not via the SSE ripple).
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    monkeypatch.setattr(
        mr, "record_user_correction",
        Mock(return_value={"status": "accepted", "version": 2, "neptune_node_id": "x"}),
    )
    monkeypatch.setattr(mr.rederive, "affected_cards", lambda concept: ["oee"])

    body = mr.CorrectMappingRequest(
        system_id="cmms", concept="mro-inventory", to_field="parts.qty_on_hand",
    )
    result = asyncio.run(mr.correct_mapping_route(body, _steward_request()))

    assert result["status"] == "accepted"
    assert dashboard._CARDS_CACHE["cards"] is None   # invalidated by the correction


def _steward_request():
    """The steward verdict ``AuthMiddleware`` stamps: the correct-mapping route
    accepts on entry only for a steward."""
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(is_admin=True))
