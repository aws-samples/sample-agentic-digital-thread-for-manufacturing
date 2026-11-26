# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ADR-0069 (follow-up measured live): the onboarding AgentCore runtime scales
to several containers and the gateway routes each tool call independently.
Only ``get_catalog`` hydrated its OWN container's in-process catalog from the
graph; every other tool trusted that it had happened — so ``get_catalog``
listed a system and ``discover`` on a sibling container answered "System X
not found" in the same conversation. ``SystemCatalog.lookup`` is the ONE seam:
on a miss it hydrates from the graph (through the single converter) exactly
once and looks again. In the app process the catalog is restored at startup,
so a hit never touches the graph."""
from __future__ import annotations

import agents.catalog as cat_mod
from agents.catalog import SystemCatalog


def _rows():
    return [{"id": "system-a", "name": "System A", "system_type": "MES",
             "plant": "site-1", "status": "onboarded", "schema": "",
             "connection_type": "PostgreSQL"}]


def test_lookup_hits_never_touch_the_graph(monkeypatch):
    calls = []
    monkeypatch.setattr(cat_mod, "neptune_query", lambda *a, **k: calls.append(1) or _rows(), raising=False)
    c = SystemCatalog([{"id": "system-a", "name": "A", "status": "onboarded"}])
    assert c.lookup("system-a")["id"] == "system-a"
    assert calls == []


def test_lookup_miss_hydrates_once_then_finds(monkeypatch):
    calls = []
    def q(*a, **k):
        calls.append(1); return _rows()
    c = SystemCatalog([])
    monkeypatch.setattr(c, "_graph_rows", q)
    entry = c.lookup("system-a")
    assert entry and entry["id"] == "system-a"
    assert len(calls) == 1
    # Second miss on a genuinely unknown id re-syncs (a system registered
    # after this container booted must become visible) — still bounded to
    # one graph read per lookup.
    assert c.lookup("system-zzz") is None
    assert len(calls) == 2


def test_lookup_graph_failure_degrades_to_miss_and_logs(monkeypatch, caplog):
    import logging
    c = SystemCatalog([])
    def boom(*a, **k):
        raise RuntimeError("graph unreachable")
    monkeypatch.setattr(c, "_graph_rows", boom)
    with caplog.at_level(logging.WARNING, logger=cat_mod.__name__):
        assert c.lookup("system-a") is None
    assert "hydration failed" in caplog.text


def test_onboarding_tools_use_lookup_not_raw_get():
    """Every system-scoped onboarding tool must go through the hydrating
    seam — a raw ``catalog.get(system_id)`` in a tool body is the exact
    bug class measured live."""
    import inspect, re
    import agents.onboard_agent as oa
    src = inspect.getsource(oa)
    raw = [m.start() for m in re.finditer(r"\bcatalog\.get\(system_id\)", src)]
    assert raw == [], f"raw catalog.get(system_id) at offsets {raw}"
