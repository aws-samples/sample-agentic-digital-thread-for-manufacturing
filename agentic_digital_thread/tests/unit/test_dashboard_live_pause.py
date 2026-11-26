# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the timer-driven SSE dashboard snapshot pause gate.

When the live-updates runtime toggle is PAUSED, the recurring ``event:
dashboard`` snapshot (:func:`agents.dashboard._snapshot_dashboard_data`) must
serve the LAST cached cards WITHOUT triggering a fresh ``_build_cards`` resolve
— so the per-viewer timer stops re-scanning the Athena/SiteWise backend while
paused. On-demand ``GET /api/dashboard`` (``fetch_dashboard_data``) is unchanged.
No AWS, no network — every resolve seam is stubbed.
"""

import pytest

import agents.dashboard as dashboard


@pytest.fixture(autouse=True)
def _reset_live_state():
    dashboard.live_settings._override = None
    dashboard.live_settings._persister = None
    yield
    dashboard.live_settings._override = None
    dashboard.live_settings._persister = None


def _stub_onboarded(monkeypatch, systems):
    monkeypatch.setattr(dashboard, "_onboarded_for_plant", lambda plant='all': systems)


def test_snapshot_serves_cached_without_resolve_when_paused(monkeypatch):
    cached = [{"card_id": "throughput", "metrics": []}]
    dashboard._CARDS_CACHE["cards"] = cached
    _stub_onboarded(monkeypatch, [{"system_type": "MES"}])

    # A fresh resolve MUST NOT happen on the timer while paused.
    def _boom():
        raise AssertionError("_build_cards must not run on the paused timer snapshot")
    monkeypatch.setattr(dashboard, "_build_cards", _boom)
    monkeypatch.setattr(dashboard.live_settings, "live_updates_enabled", lambda: False)

    snap = dashboard._snapshot_dashboard_data(plant="all")

    assert snap["cards"] == cached           # last-cached served as-is
    assert snap["connected_types"] == ["MES"]  # cheap catalog read still truthful


def test_snapshot_serves_empty_when_paused_and_cache_cold(monkeypatch):
    dashboard._CARDS_CACHE["cards"] = None
    _stub_onboarded(monkeypatch, [])
    monkeypatch.setattr(dashboard, "_build_cards",
                        lambda: (_ for _ in ()).throw(AssertionError("no resolve")))
    monkeypatch.setattr(dashboard.live_settings, "live_updates_enabled", lambda: False)

    snap = dashboard._snapshot_dashboard_data(plant="all")

    assert snap["cards"] == []               # nothing cached ⇒ empty, still no resolve
    assert snap["connected_types"] == []


def test_snapshot_resolves_when_live_updates_enabled(monkeypatch):
    # When ON, the timer snapshot delegates to fetch_dashboard_data (unchanged).
    called = {"fetch": False}

    def _fake_fetch(plant='all'):
        called["fetch"] = True
        return {"connected_types": ["MES"], "cards": [{"card_id": "x"}]}

    monkeypatch.setattr(dashboard, "fetch_dashboard_data", _fake_fetch)
    monkeypatch.setattr(dashboard.live_settings, "live_updates_enabled", lambda: True)

    snap = dashboard._snapshot_dashboard_data(plant="all")

    assert called["fetch"] is True
    assert snap["cards"] == [{"card_id": "x"}]


def test_on_demand_fetch_still_resolves_when_paused(monkeypatch):
    # The on-demand path (fetch_dashboard_data) is NOT gated by the pause toggle —
    # a user loading the page still gets a resolve (served from the TTL cache when
    # warm). Here we prove fetch_dashboard_data calls _cached_cards even paused.
    _stub_onboarded(monkeypatch, [{"system_type": "MES"}])
    resolved = [{"card_id": "throughput"}]
    monkeypatch.setattr(dashboard, "_cached_cards", lambda: resolved)
    monkeypatch.setattr(dashboard.live_settings, "live_updates_enabled", lambda: False)

    data = dashboard.fetch_dashboard_data(plant="all")

    assert data["cards"] == resolved  # on-demand resolve unaffected by the pause
