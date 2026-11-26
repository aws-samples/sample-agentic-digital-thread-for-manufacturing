# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure in-process unit tests for the S2.3 declarative dashboard ``cards`` array
(ADR 0018 / ADR 0001).

No AWS, no graph, no DB, no LLM, no fixtures, no markers — runs offline in the
default (unmarked) pytest lane, mirroring ``tests/unit/test_metric_resolver.py``.

The dashboard imports the resolver as a *module* (``from . import metric_resolver
as mr``) and calls ``mr.resolve_metric(ref)``; ``resolve_metric_route`` calls the
same module-global ``resolve_metric``. So a single ``monkeypatch.setattr(mr,
"resolve_metric", ...)`` drives BOTH the dashboard cards and the agent-tool route
— this is exactly the shared-object seam the byte-identical proof relies on.

S2.3 folds the former flag-gated S1.3 tracer card into a declarative ``cards``
array (``_build_cards`` over ``get_card_layer().list_cards()``). The two seed
cards are ``spare-parts-on-hand`` (direct metric → resolves) and ``oee`` (the
canonical *unavailable* card here). A single resolver stub *dispatches on the
``metric_id``* so both seed cards are exercised by ONE patch: spare-parts resolves
to the canned ``ResolvedMetric`` (value 18 + direct ``Lineage``), oee raises.

``fetch_dashboard_data()`` is now a *cheap* snapshot (S5.3 Wave 3): the hardcoded
per-system + ``UNIFIED_*_SQL`` entity queries are deleted (sections moved to the
lazy ``/api/dashboard/sections`` route), so it returns EXACTLY
``{'connected_types': [...], 'cards': [...]}`` and touches no DB — no ``query_pg``
patch is needed any more.
"""
from __future__ import annotations

import asyncio
import json
import queue
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.card_events as ce
import agents.dashboard as dashboard
import agents.entity_render as entity_render
import agents.metric_resolver as mr
from agents.auth.middleware import AuthMiddleware
from agents.config import get_config
from agents.dashboard import router as dashboard_router
from agents.metric_layer import Aggregation
from agents.metric_resolver import (
    AuthoritySource,
    InvariantViolation,
    Lineage,
    MappingProvenance,
    MetricResolverError,
    QueryPlan,
    ResolvedMetric,
    resolve_metric_route,
)
from agents.models import ResolveMetricRequest


# --- Test doubles / canned shapes -------------------------------------------

def _canned() -> ResolvedMetric:
    """The resolved ``spare-parts-on-hand`` metric — the shared canned shape.

    Carries a direct ``Lineage`` (S2.2) so the card and the route render the
    SAME lineage dict via ``mr._lineage_to_dict`` — the byte-identical seam.
    The ``query_plan`` object is shared between ``ResolvedMetric.query_plan`` and
    ``Lineage.query_plan`` so both render the embedded ``aggregation`` enum the
    same way (``.value``).
    """
    plan = QueryPlan(
        system_id="cmms",
        schema="cmms_maintenance",
        table="spare_parts",
        column="quantity_on_hand",
        aggregation=Aggregation.SUM,
    )
    lineage = Lineage(
        metric_id="spare-parts-on-hand",
        kind="direct",
        concept="mro-inventory",
        authority=AuthoritySource(system_id="cmms", source="single_system_map"),
        mapping=MappingProvenance(
            table="spare_parts",
            column="quantity_on_hand",
            role="measure",
            confidence=0.95,
            source="agent_proposed",
        ),
        query_plan=plan,
        sql="SELECT SUM(quantity_on_hand) FROM cmms_maintenance.spare_parts",
        executed_at="2026-06-12T00:00:00+00:00",
    )
    return ResolvedMetric(
        metric_id="spare-parts-on-hand",
        value=18,
        unit="units",
        label="Spare Parts On Hand",
        query_plan=plan,
        system_id="cmms",
        lineage=lineage,
    )


def _dispatch(metric_id, *_args, **_kwargs):
    """Single ``resolve_metric`` replacement that *dispatches on metric_id*.

    Drives every seed card through ONE patch of the shared module-global:
      • ``spare-parts-on-hand`` → the canned ``ResolvedMetric`` (value 18 + lineage).
      • ``oee``                 → raises ``MetricResolverError('boom')`` (the
                                  canonical honest-unavailable card, Decision D).
    Both ``_build_card_metric(ref)`` and ``resolve_metric_route`` pass the
    ``metric_id`` as the first positional arg, so the route and the card stay
    byte-identical for spare-parts under this same seam.
    """
    if metric_id == "spare-parts-on-hand":
        return _canned()
    if metric_id == "oee":
        raise MetricResolverError("boom")
    raise KeyError(metric_id)


def _card(result: dict, card_id: str) -> dict:
    """Pluck a card from ``result['cards']`` by its ``card_id`` (order-robust)."""
    return next(c for c in result["cards"] if c["card_id"] == card_id)


# --- Case 1: cards default-on ⇒ payload is EXACTLY {connected_types, cards} --

def test_payload_keys_exactly_connected_types_and_cards(monkeypatch):
    # S5.3 Wave 3: the hardcoded SQL + legacy top-level keys are gone — the
    # snapshot is exactly {connected_types, cards}, no flag involved.
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)

    result = dashboard.fetch_dashboard_data()

    assert set(result.keys()) == {"connected_types", "cards"}
    # No legacy / per-table / UNIFIED-derived keys survive.
    for legacy in ("indianapolis", "detroit", "orders", "suppliers",
                   "production_runs", "quality_checks", "assets"):
        assert legacy not in result
    # cards is the default mapping-driven KPI row; resolver exercised per card.
    # The scalar snapshot is the kpi + gauge cards (the quality-by-line bar is
    # excluded): spare-parts, oee, the four OT-native KPIs, then the
    # performance gauge + active-alarms KPI (new-metric-cards wave).
    assert [c["card_id"] for c in result["cards"]] == [
        "spare-parts-on-hand", "oee", "throughput", "machine-speed",
        "scrap-waste", "runtime-hours", "performance", "active-alarms"]


# --- Case 2: success ⇒ spare-parts card built from ResolvedMetric -----------

def test_success_builds_cards(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)

    result = dashboard.fetch_dashboard_data()

    assert len(result["cards"]) == 8

    card = _card(result, "spare-parts-on-hand")
    assert card["card_id"] == "spare-parts-on-hand"
    assert card["title"] == "Spare Parts On Hand"
    assert card["viz_type"] == "kpi"
    assert card["group_by"] is None

    metric = card["metrics"][0]
    assert metric["status"] == "ok"
    assert metric["value"] == 18
    assert metric["unit"] == "units"
    assert metric["system_id"] == "cmms"
    assert metric["metric_id"] == "spare-parts-on-hand"
    assert metric["lineage"] is not None


# --- Case 3: oee card unavailable, sibling spare-parts still ok -------------

def test_card_payload_carries_thresholds_and_target(monkeypatch):
    # PART A: the optional declarative presentation seams are card-level and
    # independent of metric resolution — stub _build_card_metric so no resolver
    # is touched, then assert the payload carries them only where set.
    monkeypatch.setattr(
        dashboard, "_build_card_metric",
        lambda ref, drill_sink=None: {"metric_id": ref, "status": "ok", "value": 1},
    )

    # scrap-waste carries the ordered green/orange/red bands as {value,color} dicts.
    scrap = dashboard._build_card("scrap-waste")
    assert scrap["thresholds"] == [
        {"value": 500.0, "color": "green"},
        {"value": 1000.0, "color": "orange"},
        {"value": None, "color": "red"},
    ]
    assert "target" not in scrap                     # no target on scrap-waste

    # throughput carries a numeric target and NO thresholds.
    thru = dashboard._build_card("throughput")
    assert thru["target"] == 1000.0
    assert "thresholds" not in thru

    # spare-parts carries neither ⇒ byte-identical base payload (no extra keys).
    spare = dashboard._build_card("spare-parts-on-hand")
    assert set(spare) == {"card_id", "title", "viz_type", "group_by", "metrics"}

    # The whole card payload is JSON-serializable (no dataclass/enum leftovers).
    assert json.dumps([scrap, thru, spare])


def test_oee_unavailable_sibling_ok(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)

    result = dashboard.fetch_dashboard_data()

    oee = _card(result, "oee")["metrics"][0]
    assert oee["status"] == "unavailable"
    assert oee["value"] is None
    assert "boom" in oee["error"]
    # No value ⇒ no captured lineage (Decision D).
    assert "lineage" not in oee

    # One bad ref does not poison its siblings — spare-parts still resolves ok.
    spare = _card(result, "spare-parts-on-hand")["metrics"][0]
    assert spare["status"] == "ok"
    assert spare["value"] == 18

    # Payload shape is unchanged by the failure path: exactly {connected_types, cards}.
    assert set(result.keys()) == {"connected_types", "cards"}
    assert "indianapolis" not in result
    assert "detroit" not in result


# --- Case 4: payload key set is stable and free of any deleted-SQL keys ------

def test_payload_no_legacy_or_sql_keys(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)

    # Two successive calls produce the identical (stable) key set — and that set
    # never contains any of the deleted UNIFIED_*_SQL / per-table / legacy keys.
    keys_a = set(dashboard.fetch_dashboard_data().keys())
    keys_b = set(dashboard.fetch_dashboard_data().keys())

    assert keys_a == keys_b == {"connected_types", "cards"}
    assert keys_a.isdisjoint({
        "indianapolis", "detroit", "orders", "suppliers", "work_orders",
        "products", "production_runs", "quality_checks", "downtime_events",
        "assets",
    })


# --- Case 5: byte-identical proof (ADR 0001) — one patch drives both paths ---

def test_byte_identical_card_and_route(monkeypatch):
    # Single patch of the shared object drives BOTH the dashboard card and the
    # /api/metrics/resolve route ⇒ identical value AND lineage by construction.
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)

    result = dashboard.fetch_dashboard_data()
    spare_metric = _card(result, "spare-parts-on-hand")["metrics"][0]
    route = asyncio.run(
        resolve_metric_route(ResolveMetricRequest(metric_id="spare-parts-on-hand"))
    )

    # Value parity (the original ADR 0001 byte-identical proof).
    assert spare_metric["value"] == route["value"] == 18

    # Lineage parity — present on the card and equal to the route's lineage dict,
    # both rendered via the SAME ``mr._lineage_to_dict`` so they cannot diverge.
    assert spare_metric["lineage"] is not None
    assert spare_metric["lineage"] == route["lineage"]

    # Spot-check the captured chain: metric → system → table.column → SQL.
    lin = spare_metric["lineage"]
    assert lin["kind"] == "direct"
    assert lin["metric_id"] == "spare-parts-on-hand"
    assert lin["authority"]["system_id"] == "cmms"
    assert lin["mapping"]["table"] == "spare_parts"
    assert lin["mapping"]["column"] == "quantity_on_hand"
    assert lin["mapping"]["source"] == "agent_proposed"
    assert lin["sql"] == "SELECT SUM(quantity_on_hand) FROM cmms_maintenance.spare_parts"
    # Embedded enum rendered as its ``.value`` — no enum objects survive.
    assert lin["query_plan"]["aggregation"] == "sum"
    # The whole record is JSON-serializable (no enum/dataclass leftovers).
    json.dumps(lin)


# --- Case 6: GATED live visual — per-card #kpi-card-<id>-lineage (skips) -----

@pytest.mark.skip(
    reason="GATED live visual: the per-card #kpi-card-<id>-lineage expander "
    "render is verified live (Playwright) against a running stack, mirroring "
    "the S1.3/S2.2 live tracer-card e2e. Skips cleanly offline; never "
    "hard-fails in the default lane."
)
def test_lineage_panel_renders_live_gated():  # pragma: no cover
    """Placeholder for the live, flag-gated multi-card lineage-panel visual.

    When run live (authed dashboard), each rendered card exposes its own
    ``#kpi-card-<card_id>-lineage`` ``<details>`` expander showing the chain
    metric → system_id → table.column → SQL (plus role/confidence/source),
    recursing for derived lineage. This is intentionally GATED/SKIPPED in the
    offline lane — consistent with S1.3/S2.2 cloud-lane gating — because it
    requires a running stack and a browser; the offline DOM contract is already
    covered by the dashboard.js no-cards/byte-identical guards.
    """
    pytest.skip("live stack required")


# ===========================================================================
# S3.2 / W2.3 — _build_card refactor + dashboard_stream card_update drain
# ===========================================================================

# --- Case 7: _build_cards byte-identical after the _build_card refactor ------

def test_build_cards_delegates_to_build_card_byte_identical(monkeypatch):
    # The refactor splits per-card resolution into _build_card; _build_cards must
    # be exactly [_build_card(c.id) for c in list_cards()] — output unchanged.
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)

    via_build_cards = dashboard._build_cards()
    manual = [
        dashboard._build_card("spare-parts-on-hand"),
        dashboard._build_card("oee"),
        dashboard._build_card("throughput"),
        dashboard._build_card("machine-speed"),
        dashboard._build_card("scrap-waste"),
        dashboard._build_card("runtime-hours"),
        dashboard._build_card("performance"),
        dashboard._build_card("active-alarms"),
    ]
    assert via_build_cards == manual
    assert [c["card_id"] for c in via_build_cards] == [
        "spare-parts-on-hand", "oee", "throughput", "machine-speed",
        "scrap-waste", "runtime-hours", "performance", "active-alarms"]
    # Stable, JSON-serializable shape (no enum/dataclass leftovers).
    assert json.dumps(via_build_cards)

    # _build_card returns the same per-card dict shape as before the refactor,
    # plus the Wave 1 `drill` hint (spare-parts resolves to a physical query_plan
    # under _dispatch, so it carries a click-to-drill target).
    spare = dashboard._build_card("spare-parts-on-hand")
    _base = {"card_id", "title", "viz_type", "group_by", "metrics"}
    assert _base <= set(spare)
    assert set(spare) - _base <= {"drill", "context"}  # optional keys only
    assert spare["drill"]["table"]  # (system_id, table) drill target present
    assert spare["card_id"] == "spare-parts-on-hand"
    assert spare["viz_type"] == "kpi"
    assert spare["metrics"][0]["value"] == 18


# --- Case 8: RESOLVER_REDERIVE on ⇒ dashboard_stream yields a card_update -----

def test_dashboard_stream_emits_card_update_when_flag_on(monkeypatch):
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    ce.reset()

    # One pre-seeded mqtt item so the loop's q.get(timeout=1) returns at once
    # (deterministic; no 1s blocking before the card_events drain runs).
    fake_q: queue.Queue = queue.Queue()
    fake_q.put_nowait({"topic": "adt/test", "payload": "x"})
    monkeypatch.setattr(dashboard.mqtt_subscriber, "subscribe", lambda: fake_q)
    monkeypatch.setattr(dashboard.mqtt_subscriber, "unsubscribe", lambda q: None)
    monkeypatch.setattr(dashboard.mqtt_subscriber, "get_stats", lambda: {})

    async def drive() -> list[str]:
        resp = await dashboard.dashboard_stream(plant="all")
        it = resp.body_iterator
        frames: list[str] = [await it.__anext__()]  # first: event: dashboard
        # The generator has now subscribed to card_events; publish a batch.
        ce.publish({
            "event": "card_update", "status": "updated",
            "cards": [{"card_id": "oee", "metrics": []}],
            "audit": {"text": "updated from correction · mapping v2"},
        })
        for _ in range(6):
            frames.append(await it.__anext__())
            if any(f.startswith("event: card_update") for f in frames):
                break
        await it.aclose()
        return frames

    frames = asyncio.run(drive())
    ce.reset()

    card_updates = [f for f in frames if f.startswith("event: card_update")]
    assert card_updates, f"no card_update frame emitted; got {frames!r}"
    # SSE framing + JSON round-trip.
    frame = card_updates[0]
    assert frame.endswith("\n\n")
    data = frame.split("data: ", 1)[1].strip()
    payload = json.loads(data)
    assert payload["status"] == "updated"
    assert payload["cards"][0]["card_id"] == "oee"


# --- Case 9: flag OFF ⇒ generator never subscribes to card_events ------------

def test_dashboard_stream_no_card_events_subscribe_when_flag_off(monkeypatch):
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)

    subscribe_spy = Mock(name="card_events.subscribe")
    monkeypatch.setattr(dashboard.card_events, "subscribe", subscribe_spy)

    fake_q: queue.Queue = queue.Queue()
    fake_q.put_nowait({"topic": "adt/test", "payload": "x"})
    monkeypatch.setattr(dashboard.mqtt_subscriber, "subscribe", lambda: fake_q)
    monkeypatch.setattr(dashboard.mqtt_subscriber, "unsubscribe", lambda q: None)
    monkeypatch.setattr(dashboard.mqtt_subscriber, "get_stats", lambda: {})

    async def drive() -> list[str]:
        resp = await dashboard.dashboard_stream(plant="all")
        it = resp.body_iterator
        frames = [await it.__anext__()]            # dashboard frame
        frames.append(await it.__anext__())        # mqtt frame
        await it.aclose()
        return frames

    frames = asyncio.run(drive())

    # Flag off ⇒ cq is None ⇒ card_events.subscribe is never called and no
    # card_update frame is ever emitted (SSE byte-stream identical to today).
    subscribe_spy.assert_not_called()
    assert not any(f.startswith("event: card_update") for f in frames)


# ===========================================================================
# S4.2 — proactive coverage gating behind RESOLVER_COVERAGE
# ===========================================================================
#
# A SEPARATE env flag (RESOLVER_COVERAGE) gates the proactive `uncovered` state.
# Flag off ⇒ covered_set=None ⇒ today's byte-identical fall-through path. These
# cases drive the flag and patch the coverage module's `covered_concepts`
# graph-read seam (dashboard does `from . import coverage`, so dashboard.coverage
# IS the module object — patching it patches what _build_cards calls).


# --- Case 10: RESOLVER_COVERAGE on + empty covered ⇒ uncovered, NO resolve ---

def test_coverage_flag_on_uncovered_no_resolve(monkeypatch):
    monkeypatch.setenv("RESOLVER_COVERAGE", "1")
    # Empty onboarded set ⇒ every seed card is structurally uncovered.
    monkeypatch.setattr(dashboard.coverage, "covered_concepts", lambda: frozenset())
    # resolve_metric must NEVER be called for an uncovered card (no fake value).
    spy = Mock(name="resolve_metric")
    monkeypatch.setattr(mr, "resolve_metric", spy)

    result = dashboard.fetch_dashboard_data()

    oee = _card(result, "oee")
    assert oee["status"] == "uncovered"
    assert oee["missing_concepts"]                       # non-empty structural gap
    assert oee["metrics"] == []                          # no resolve attempted
    assert oee["suggestion"].startswith("Onboard a system providing")
    assert oee["suggestions"][0].startswith("Onboard a system providing")
    # spare-parts (direct mro-inventory) is also uncovered under an empty set.
    spare = _card(result, "spare-parts-on-hand")
    assert spare["status"] == "uncovered"
    assert spare["metrics"] == []
    # The whole point of proactive gating: resolve_metric was never invoked.
    spy.assert_not_called()
    # uncovered payload is JSON-serializable (no enum/dataclass leftovers).
    assert json.dumps(result["cards"])


# --- Case 11: RESOLVER_COVERAGE on + fully covered ⇒ fall-through (ok/unavail) ---

def test_coverage_flag_on_covered_falls_through(monkeypatch):
    monkeypatch.setenv("RESOLVER_COVERAGE", "1")
    # Cover spare-parts (mro-inventory) AND the oee GAUGE card's refs — the
    # composite oee (via its inputs) plus the sub-readings availability /
    # performance / the oee-quality yield (concepts quality-metric + actual-quantity)
    # ⇒ both cards satisfiable ⇒ fall through to the EXISTING reactive resolve path.
    monkeypatch.setattr(
        dashboard.coverage, "covered_concepts",
        lambda: frozenset({"mro-inventory", "availability", "performance",
                           "quality-metric", "actual-quantity"}),
    )
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)

    result = dashboard.fetch_dashboard_data()

    # Satisfiable cards carry NO structural-gap keys — covered output shape is
    # the covered-today shape (distinct from `uncovered`).
    spare = _card(result, "spare-parts-on-hand")
    assert "status" not in spare
    assert "missing_concepts" not in spare
    assert spare["metrics"][0]["status"] == "ok"
    assert spare["metrics"][0]["value"] == 18

    # oee falls through to resolve and yields the REACTIVE `unavailable` per-metric
    # state (runtime resolve failure) — distinct from structural `uncovered`.
    oee = _card(result, "oee")
    assert "status" not in oee                           # no card-level uncovered
    assert oee["metrics"][0]["status"] == "unavailable"
    assert oee["metrics"][0]["value"] is None


# --- Case 12: byte-identical ⇒ flag OFF never reads coverage, shape unchanged ---

def test_coverage_flag_off_byte_identical(monkeypatch):
    monkeypatch.delenv("RESOLVER_COVERAGE", raising=False)
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)

    # If the flag-off path touched the coverage seam at all, this would explode.
    def _boom():
        raise AssertionError("covered_concepts must NOT be read when flag is off")
    monkeypatch.setattr(dashboard.coverage, "covered_concepts", _boom)

    result = dashboard.fetch_dashboard_data()

    # Output is byte-identical to the non-gated manual build (covered_set=None),
    # i.e. exactly today's payload — no `status`/`missing_concepts`/`suggestion` keys.
    manual = [
        dashboard._build_card("spare-parts-on-hand"),
        dashboard._build_card("oee"),
        dashboard._build_card("throughput"),
        dashboard._build_card("machine-speed"),
        dashboard._build_card("scrap-waste"),
        dashboard._build_card("runtime-hours"),
        dashboard._build_card("performance"),
        dashboard._build_card("active-alarms"),
    ]
    assert result["cards"] == manual
    for card in result["cards"]:
        # Base scalar card keys are always present; OT KPI cards MAY additionally
        # carry the optional declarative presentation seams (thresholds/target)
        # and the Wave 1 `drill` hint (present when a direct metric resolves to a
        # physical (system_id, table) — spare-parts/throughput/etc. under _dispatch).
        base = {"card_id", "title", "viz_type", "group_by", "metrics"}
        assert base <= set(card)
        assert set(card) - base <= {"thresholds", "target", "drill", "context"}


# ===========================================================================
# S5.3 / Wave 2 — honest connected_types + lazy /api/dashboard/sections route
# ===========================================================================
#
# connected_types is derived ONLY from catalog.get_onboarded() system_types
# (the hardcoded UNIFIED_*_SQL queries are gone as of Wave 3). The dashboard
# `from .catalog import catalog` makes `dashboard.catalog` the singleton
# object — patching its `get_onboarded` drives the derivation deterministically.


# --- Case 13: zero onboarded systems ⇒ connected_types == [] (honest empty) ---

def test_connected_types_empty_when_none_onboarded(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: [])

    result = dashboard.fetch_dashboard_data()

    # Honest empty state — connected_types comes solely from onboarded systems.
    assert result["connected_types"] == []


# --- Case 14: onboarded ERP + MES ⇒ connected_types == ['ERP','MES'] (sorted) ---

def test_connected_types_derived_from_onboarded(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)
    fake_onboarded = [
        {"id": "erp", "name": "ERP", "plant": "All Sites",
         "system_type": "ERP", "schema": "erp_data"},
        {"id": "mes-mil", "name": "Milwaukee MES", "plant": "Milwaukee",
         "system_type": "MES", "schema": "mes_milwaukee"},
    ]
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: fake_onboarded)

    result = dashboard.fetch_dashboard_data()

    # Derived purely from onboarded system_types, sorted.
    assert result["connected_types"] == ["ERP", "MES"]


# --- /api/dashboard/sections lazy route (AuthMiddleware-gated) ---------------

def _build_sections_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(dashboard_router)
    return app


def test_sections_route_requires_auth(monkeypatch):
    # /api/dashboard/sections is session-authed (not public, not /api/admin) ⇒
    # 401 without a session, exactly like the other /api/* routes.
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_sections_app())

    resp = client.get("/api/dashboard/sections")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_sections_route_returns_rendered_sections(monkeypatch, local_auth_env):
    # DEV_NO_AUTH grants a session; render_all_sections is mocked so the route is
    # pure-unit (no graph, no DB, no AWS). The route just wraps the rendered list.
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()
    sentinel = [{
        "system_id": "erp", "system_name": "ERP", "entity": "sales-order",
        "entity_label": "Sales Order", "table": "customer_orders", "kind": "table",
        "columns": [{"column": "order_number", "label": "Order Number",
                     "field_concept": "order-id"}],
        "rows": [["SO-1001"]], "row_count": 1, "status": "ok",
    }]
    monkeypatch.setattr(entity_render, "render_all_sections", lambda systems: sentinel)
    client = TestClient(_build_sections_app())

    resp = client.get("/api/dashboard/sections")
    assert resp.status_code == 200
    assert resp.json() == {"sections": sentinel}


# ===========================================================================
# W2 BACKEND — OEE gauge card + quality-by-line bar kept out of the scalar path
# ===========================================================================
#
# The `oee` card is now a GAUGE whose ordered metric_refs are (oee, availability,
# performance, quality-rate) — _build_card resolves ALL of them into metrics[].
# The `quality-by-line` BAR card carries a group_by and is EXCLUDED from the
# scalar _build_cards snapshot (resolved client-side via the by-dimension route).


def _gauge_dispatch(metric_id, *_a, **_k):
    """Resolve each OEE-gauge ref to a distinct canned ResolvedMetric so the
    gauge card's metrics[] order + per-metric value/status/lineage are checked."""
    values = {
        "oee": 0.72, "availability": 0.9, "performance": 0.85, "oee-quality": 0.94,
    }
    if metric_id not in values:
        raise MetricResolverError(f"unexpected ref {metric_id!r}")
    plan = QueryPlan(system_id="mes-mil", schema="mes_milwaukee",
                     table="oee_metrics", column=metric_id.replace("-", "_"),
                     aggregation=Aggregation.AVG)
    lineage = Lineage(
        metric_id=metric_id, kind="direct", concept=metric_id,
        authority=AuthoritySource(system_id="mes-mil", source="authority_rule"),
        mapping=MappingProvenance(table="oee_metrics", column="c", role="measure",
                                  confidence=0.9, source="agent_proposed"),
        query_plan=plan, sql="SELECT AVG(c) FROM mes_milwaukee.oee_metrics",
        executed_at="2026-06-17T00:00:00+00:00",
    )
    return ResolvedMetric(metric_id=metric_id, value=values[metric_id], unit="ratio",
                          label=metric_id, query_plan=plan, system_id="mes-mil",
                          lineage=lineage)


def test_oee_gauge_card_resolves_four_ordered_metrics(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric", _gauge_dispatch)

    card = dashboard._build_card("oee")
    assert card["card_id"] == "oee"
    assert card["viz_type"] == "gauge"
    assert card["group_by"] is None

    # metrics[] is the ordered gauge content: composite OEE first, then the three
    # sub-readings — each carrying value + status + lineage.
    assert [m["metric_id"] for m in card["metrics"]] == [
        "oee", "availability", "performance", "oee-quality"]
    for m in card["metrics"]:
        assert m["status"] == "ok"
        assert m["value"] is not None
        assert m["lineage"] is not None
    assert card["metrics"][0]["value"] == 0.72


def test_build_cards_excludes_bar_group_by_card(monkeypatch):
    # _build_cards is the SCALAR snapshot: it returns exactly the kpi + gauge
    # cards and NEVER the group_by `quality-by-line` bar (resolved client-side
    # via /api/metrics/resolve-by-dimension). No error is raised.
    monkeypatch.setattr(mr, "resolve_metric", _gauge_dispatch)
    monkeypatch.setattr(
        dashboard, "_build_card_metric",
        lambda ref, drill_sink=None: {"metric_id": ref, "status": "ok", "value": 1},
    )

    cards = dashboard._build_cards()
    ids = [c["card_id"] for c in cards]
    assert ids == ["spare-parts-on-hand", "oee", "throughput", "machine-speed",
                   "scrap-waste", "runtime-hours", "performance", "active-alarms"]
    assert "quality-by-line" not in ids
    assert "machine-state" not in ids               # the machine-state bar is a group_by card too
    assert "inspections-by-result" not in ids       # the inspection-result bar is a group_by card too
    # kpi + gauge only; the bar card never appears in the scalar snapshot.
    assert {c["viz_type"] for c in cards} == {"kpi", "gauge"}
    assert json.dumps(cards)  # JSON-serializable, no enum/dataclass leftovers


def test_build_card_group_by_card_returns_descriptor_without_resolve(monkeypatch):
    # The shared per-card path (also used by the re-derivation worker) must skip
    # the scalar resolve for a group_by card and emit a declarative descriptor.
    spy = Mock(name="resolve_metric")
    monkeypatch.setattr(mr, "resolve_metric", spy)

    card = dashboard._build_card("quality-by-line")
    assert card["card_id"] == "quality-by-line"
    assert card["viz_type"] == "bar"
    assert card["group_by"] == "production-line"
    # Now uses quality-yield-rate (0-100 rate), NOT quality-rate (good-count)
    assert card["metric_refs"] == ["quality-yield-rate"]
    assert card["resolver"] == "by-dimension"
    assert card["metrics"] == []
    # No scalar resolve attempted for a by-dimension card.
    spy.assert_not_called()


# --- S5.4 cost control — TTL cache for resolved cards ------------------------

def test_cached_cards_serves_from_cache_within_ttl(monkeypatch):
    """``_cached_cards`` resolves once and serves the cache on repeat calls within the TTL
    (so the SSE snapshot does not re-scan the backend every tick), and re-resolves after
    ``invalidate_cards_cache``. Guards the Athena-cost regression."""
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "9999")
    dashboard.invalidate_cards_cache()

    calls = {"n": 0}
    sentinel = [{"card_id": "x"}]

    def _fake_build():
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(dashboard, "_build_cards", _fake_build)

    first = dashboard._cached_cards()
    second = dashboard._cached_cards()
    assert first is second is sentinel
    assert calls["n"] == 1, "second call within TTL must hit the cache, not re-resolve"

    dashboard.invalidate_cards_cache()
    dashboard._cached_cards()
    assert calls["n"] == 2, "invalidation must force a re-resolve"


def test_cached_cards_ttl_zero_disables_cache(monkeypatch):
    """``DASHBOARD_CARDS_TTL=0`` disables caching — every call resolves fresh (opt-out)."""
    monkeypatch.setenv("DASHBOARD_CARDS_TTL", "0")
    dashboard.invalidate_cards_cache()

    calls = {"n": 0}

    def _fake_build():
        calls["n"] += 1
        return []

    monkeypatch.setattr(dashboard, "_build_cards", _fake_build)
    dashboard._cached_cards()
    dashboard._cached_cards()
    assert calls["n"] == 2


# --- S5.4 card context (site/cell/system) -----------------------------------

def test_system_context_helper(monkeypatch):
    """_system_context auto-derives {system, site} from the onboarded system; the wildcard
    plant 'all' yields no site; unknown/None ids yield {}."""
    monkeypatch.setattr(dashboard.catalog, "get_all", lambda: [
        {"id": "s1", "name": "Sys One", "plant": "all"},
        {"id": "s2", "name": "Sys Two", "plant": "Plant B"},
    ])
    assert dashboard._system_context("s1") == {"system": "Sys One"}
    assert dashboard._system_context("s2") == {"system": "Sys Two", "site": "Plant B"}
    assert dashboard._system_context("nope") == {}
    assert dashboard._system_context(None) == {}


def test_card_context_auto_derived_from_resolving_system(monkeypatch):
    """A card's context auto-derives {system, site} from the resolving system's id — no
    declared context needed (spare-parts resolves to system_id 'cmms' under _dispatch)."""
    monkeypatch.setattr(mr, "resolve_metric", _dispatch)
    monkeypatch.setattr(dashboard.catalog, "get_all",
                        lambda: [{"id": "cmms", "name": "CMMS Prod", "plant": "SITE9"}])
    spare = dashboard._build_card("spare-parts-on-hand")
    assert spare["context"] == {"system": "CMMS Prod", "site": "SITE9"}


# ===========================================================================
# 2A — honest-data predicate (`no-data`) + conditional `sample_count` key
# ===========================================================================
#
# `_build_card_metric` classifies a SUCCESSFUL resolve with no usable data
# (value None, or a reported sample_count of 0) as the NEW status "no-data" —
# structurally distinct from `unavailable` (resolver boundary error). The
# `sample_count` key is CONDITIONAL: present only when the resolver reported
# one, so legacy/mocked resolves (sample_count None) keep the exact key set.

import dataclasses as _dc


def _canned_with(**overrides):
    return _dc.replace(_canned(), **overrides)


def test_ok_entry_has_no_sample_count_key_when_unreported(monkeypatch):
    # Default (sample_count None) ⇒ the ok entry's key set is byte-identical to
    # the pre-2A shape — the exact-key-set discipline for single-system envs.
    monkeypatch.setattr(mr, "resolve_metric", lambda *a, **k: _canned())
    entry = dashboard._build_card_metric("spare-parts-on-hand")
    assert entry["status"] == "ok"
    assert set(entry) == {"metric_id", "label", "value", "unit", "system_id",
                          "status", "lineage"}


def test_ok_entry_carries_conditional_sample_count(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric",
                        lambda *a, **k: _canned_with(sample_count=7))
    entry = dashboard._build_card_metric("spare-parts-on-hand")
    assert entry["status"] == "ok"
    assert entry["value"] == 18
    assert entry["sample_count"] == 7


def test_null_value_classified_no_data(monkeypatch):
    # A successful resolve whose value is None (e.g. AVG over zero rows) is
    # honest "no-data" — never a fake/zero value, lineage still shows WHERE we
    # looked.
    monkeypatch.setattr(mr, "resolve_metric",
                        lambda *a, **k: _canned_with(value=None))
    entry = dashboard._build_card_metric("spare-parts-on-hand")
    assert entry["status"] == "no-data"
    assert entry["value"] is None
    assert entry["lineage"] is not None
    assert "sample_count" not in entry          # unreported ⇒ key absent
    assert "error" not in entry                 # not a failure — a data gap


def test_invariant_violation_is_not_conflated_with_no_data(monkeypatch):
    violation = InvariantViolation(
        reason="invariant_violation",
        detail="value outside declared valid range",
        offending_value=101,
    )
    lineage = _dc.replace(
        _canned().lineage,
        valid_range=(0.0, 100.0),
        max_exclusive=True,
        invariant_violation=violation,
    )
    monkeypatch.setattr(
        mr,
        "resolve_metric",
        lambda *a, **k: _canned_with(
            value=None,
            sample_count=4,
            status="invariant_violation",
            detail=violation.detail,
            lineage=lineage,
        ),
    )
    sink: dict = {}

    entry = dashboard._build_card_metric("spare-parts-on-hand", sink)

    assert entry["status"] == "invariant-violation"
    assert entry["value"] is None
    assert entry["detail"] == "value outside declared valid range"
    assert entry["sample_count"] == 4
    assert entry["lineage"]["invariant_violation"]["offending_value"] == 101
    assert sink == {}


def test_unresolved_predicate_is_not_conflated_with_no_data(monkeypatch):
    detail = ("predicate concept 'widget-color' has no field bound on system "
              "'sys-a' table 'widget_events'")
    monkeypatch.setattr(
        mr,
        "resolve_metric",
        lambda *a, **k: _canned_with(
            value=None,
            sample_count=None,
            status=mr.UNRESOLVED_PREDICATE_STATUS,
            detail=detail,
        ),
    )
    sink: dict = {}

    entry = dashboard._build_card_metric("spare-parts-on-hand", sink)

    assert entry["status"] == "unresolved-predicate"
    assert entry["value"] is None
    assert entry["detail"] == detail and "widget-color" in entry["detail"]
    assert "error" not in entry
    assert sink == {}


def test_ambiguous_binding_is_not_conflated_with_no_data(monkeypatch):
    """ADR-0076 D3: a concept bound to 2+ columns renders ``ambiguous-binding``
    with the candidates NAMED — never no-data, never a drill target."""
    detail = ("concept 'widget-kind' is bound to 2 distinct columns on system "
              "'sys-a' (widget_events: 'kind_code', 'parent.kind_code')")
    monkeypatch.setattr(
        mr, "resolve_metric",
        lambda *a, **k: _canned_with(
            value=None, sample_count=None,
            status=mr.AMBIGUOUS_BINDING_STATUS, detail=detail),
    )
    sink: dict = {}

    entry = dashboard._build_card_metric("spare-parts-on-hand", sink)

    assert entry["status"] == "ambiguous-binding"
    assert entry["value"] is None and entry["detail"] == detail
    assert "error" not in entry
    assert sink == {}


def test_zero_sample_count_classified_no_data(monkeypatch):
    # Mocked 0-row AVG: the paired COUNT reports 0 ⇒ no-data even if the
    # backend returned a degenerate value.
    monkeypatch.setattr(mr, "resolve_metric",
                        lambda *a, **k: _canned_with(value=None, sample_count=0))
    entry = dashboard._build_card_metric("spare-parts-on-hand")
    assert entry["status"] == "no-data"
    assert entry["sample_count"] == 0


def test_no_data_metric_does_not_stamp_drill(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric",
                        lambda *a, **k: _canned_with(value=None, sample_count=0))
    sink: dict = {}
    dashboard._build_card_metric("spare-parts-on-hand", sink)
    assert sink == {}                            # nothing to drill into


def test_zero_value_with_samples_stays_ok(monkeypatch):
    # An honest ZERO (value 0 with samples behind it) is OK data, not no-data —
    # the 2B idle-annotation rule depends on this distinction.
    monkeypatch.setattr(mr, "resolve_metric",
                        lambda *a, **k: _canned_with(value=0, sample_count=42))
    entry = dashboard._build_card_metric("spare-parts-on-hand")
    assert entry["status"] == "ok"
    assert entry["value"] == 0
    assert entry["sample_count"] == 42
