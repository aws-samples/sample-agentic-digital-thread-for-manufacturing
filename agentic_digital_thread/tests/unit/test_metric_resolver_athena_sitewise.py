# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for Athena + SiteWise DIRECT metric execution (ADR 0015).

Extends the deterministic metric resolver so a correctly-mapped Athena or SiteWise
(Direct) system can back a concept-driven dashboard card:

* **Athena** — the already-composed aggregate ``SELECT`` is valid Athena SQL and is
  run through the SAME connector Mode A/B use (``athena_tools.run_athena_query``),
  stubbed here — NO live AWS.
* **SiteWise (Direct)** — non-SQL: the scalar is resolved via the ``sitewise_read``
  seam (models → assets → property values), aggregated per the metric's Aggregation
  (AVG/SUM/COUNT/MIN/MAX/LATEST). The seam is fully stubbed — NO live AWS/network.

Fully offline (no AWS, graph, DB, LLM): the ``catalog``, ``neptune_query``,
``athena_tools.run_athena_query`` and the three ``sitewise_read`` helpers are
monkeypatched with canned shapes, mirroring ``test_metric_resolver.py``.
"""
from __future__ import annotations

import json

import pytest

import agents.connection_types as ct
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, CodeMetricLayer, DirectMetric
from agents.metric_resolver import (
    AuthorityResolver,
    MetricResolverError,
    resolve_metric,
)


# --- Test doubles -----------------------------------------------------------

class _FakeAuthority(AuthorityResolver):
    """Injectable authority resolver returning a fixed system id."""

    def __init__(self, system_id: str | None) -> None:
        self._system_id = system_id

    def authoritative_system(self, concept: str) -> str | None:
        return self._system_id


@pytest.fixture
def seed_catalog():
    """Seed the shared catalog with an Athena + a SiteWise (Direct) system, then
    restore the previous catalog. Base mode ships an EMPTY live catalog (ADR-0030)."""
    from agents.catalog import catalog

    saved = catalog._systems
    catalog._systems = [
        {
            "id": "athena-sys", "name": "Athena Lake",
            "connection_type": ct.ATHENA, "schema": "analytics_db",
            "status": "onboarded",
            "connection_config": {"workgroup": "wg", "database": "analytics_db"},
        },
        {
            "id": "sitewise-sys", "name": "SiteWise Direct",
            "connection_type": ct.SITEWISE_DIRECT, "schema": "",
            "status": "onboarded",
            "connection_config": {"region": "us-east-1"},
        },
    ]
    try:
        yield catalog
    finally:
        catalog._systems = saved


def _maps_to(table: str, column: str):
    """Canned neptune_query MAPS_TO traversal row for _resolve_table_column.

    Concept-aware: the event-time concept (queried by the Athena time-window
    fallback's ``_resolve_order_column``) maps to NOTHING, so these scenarios
    stay 'no time column mapped' and the composed SQL is unchanged."""
    def nq(cypher, params=None):
        from agents.metric_dimension import TIME_CONCEPT
        if (params or {}).get("concept") == TIME_CONCEPT:
            return []
        return [{"table": table, "column": column}]
    return nq


def _stub_sitewise(monkeypatch, *, models, assets, values):
    """Stub the three sitewise_read helpers the resolver calls (no AWS).

    The scalar sweep reads via the BATCH seam (event-loop fix:
    ``batch_get_property_values`` replaced the per-asset sequential reads) —
    the stub answers batch entries from the same per-asset ``values`` fixture."""
    monkeypatch.setattr(
        mr.sitewise_read, "list_asset_models_with_properties",
        lambda connection_config, region=None, **k: models,
    )
    monkeypatch.setattr(
        mr.sitewise_read, "list_assets",
        lambda connection_config, region=None, asset_model_id="", **k: assets,
    )
    monkeypatch.setattr(
        mr.sitewise_read, "batch_get_property_values",
        lambda connection_config, region, entries: {
            f"{e['assetId']}_{e['propertyId']}": values[e["assetId"]]
            for e in entries
        },
    )


# --- (A) Athena execution (SQL) ---------------------------------------------

def test_athena_direct_metric_executes_composed_sql(monkeypatch, seed_catalog):
    """An Athena system resolves a DIRECT metric: the composed aggregate SELECT is
    handed to the stubbed Athena connector, the returned row → the coerced scalar,
    and the lineage carries the SQL."""
    metric = DirectMetric(
        id="m-athena", concept="widget-count",
        aggregation=Aggregation.SUM, label="Widgets", unit="units",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("widgets", "qty"))

    captured: dict = {}

    def fake_run(sql, connection_config, **kwargs):
        captured["sql"] = sql
        captured["cfg"] = connection_config
        return {"columns": ["_col0"], "rows": [["42"]], "row_count": 1}

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", fake_run)

    resolved = resolve_metric("m-athena", authority=_FakeAuthority("athena-sys"))

    expected_sql = "SELECT SUM(qty), COUNT(qty) FROM analytics_db.widgets"
    assert captured["sql"] == expected_sql          # the connector received the SQL
    assert captured["cfg"] == {"workgroup": "wg", "database": "analytics_db"}
    assert resolved.value == 42                     # coerced from the "42" cell
    assert resolved.system_id == "athena-sys"
    assert resolved.lineage.sql == expected_sql     # lineage carries the SQL
    assert resolved.query_plan.aggregation is Aggregation.SUM


def test_athena_empty_result_fails_loud(monkeypatch, seed_catalog):
    """An empty Athena result propagates as MetricResolverError (no silent fallback)."""
    metric = DirectMetric(
        id="m-athena", concept="widget-count",
        aggregation=Aggregation.SUM, label="Widgets", unit="units",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("widgets", "qty"))
    monkeypatch.setattr(
        mr.athena_tools, "run_athena_query",
        lambda sql, connection_config, **k: {"columns": [], "rows": [], "row_count": 0},
    )

    with pytest.raises(MetricResolverError, match="empty result"):
        resolve_metric("m-athena", authority=_FakeAuthority("athena-sys"))


def test_athena_connector_error_propagates(monkeypatch, seed_catalog):
    """A connector-level failure propagates (errors PROPAGATE — no fake value)."""
    metric = DirectMetric(
        id="m-athena", concept="widget-count",
        aggregation=Aggregation.SUM, label="Widgets", unit="units",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("widgets", "qty"))

    def boom(sql, connection_config, **k):
        raise RuntimeError("Athena query failed: boom")

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", boom)

    with pytest.raises(RuntimeError, match="boom"):
        resolve_metric("m-athena", authority=_FakeAuthority("athena-sys"))


# --- (B) SiteWise (non-SQL) scalar resolution -------------------------------

_SW_MODELS = [{
    "id": "model-1", "name": "Pump",
    "properties": [{"id": "prop-temp", "name": "Temperature"}],
}]
_SW_ASSETS = [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]
# Values 10/20/30; a2 (20) has the NEWEST timestamp (200).
_SW_VALUES = {
    "a1": {"value": 10, "timestamp": 100, "quality": "GOOD"},
    "a2": {"value": 20, "timestamp": 200, "quality": "GOOD"},
    "a3": {"value": 30, "timestamp": 150, "quality": "GOOD"},
}


@pytest.mark.parametrize("agg,expected", [
    (Aggregation.SUM, 60),      # 10+20+30
    (Aggregation.AVG, 20),      # 60/3
    (Aggregation.COUNT, 3),     # three usable assets
    (Aggregation.MIN, 10),
    (Aggregation.MAX, 30),
    (Aggregation.LATEST, 20),   # a2 has the newest timestamp — does NOT raise
])
def test_sitewise_aggregation_across_assets(monkeypatch, seed_catalog, agg, expected):
    """AVG/SUM/COUNT/MIN/MAX/LATEST across multiple assets → the correct scalar.

    LATEST notably does NOT raise (it resolves via the sitewise seam, proving it
    bypasses _compose_sql, which would raise NotImplementedError on LATEST)."""
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=agg, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    _stub_sitewise(monkeypatch, models=_SW_MODELS, assets=_SW_ASSETS, values=_SW_VALUES)

    resolved = resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))

    assert resolved.value == expected
    assert resolved.system_id == "sitewise-sys"
    assert resolved.lineage.sql == f"sitewise:{agg.value}(Pump.Temperature)"
    assert resolved.query_plan.aggregation is agg


def test_sitewise_latest_does_not_go_through_compose_sql(monkeypatch, seed_catalog):
    """Explicit guard: even if _compose_sql would raise on LATEST, the SiteWise
    branch never calls it — resolution succeeds."""
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.LATEST, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    _stub_sitewise(monkeypatch, models=_SW_MODELS, assets=_SW_ASSETS, values=_SW_VALUES)

    def boom(plan):
        raise AssertionError("_compose_sql must not be called for SiteWise LATEST")

    monkeypatch.setattr(mr, "_compose_sql", boom)

    resolved = resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))
    assert resolved.value == 20


def test_sitewise_name_resolution_is_case_insensitive(monkeypatch, seed_catalog):
    """Model + property names match case-insensitively against discovery names."""
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.MAX, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    # Mapped names differ ONLY in case from the discovered names.
    monkeypatch.setattr(mr, "neptune_query", _maps_to("pump", "temperature"))
    _stub_sitewise(monkeypatch, models=_SW_MODELS, assets=_SW_ASSETS, values=_SW_VALUES)

    resolved = resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))
    assert resolved.value == 30


def test_sitewise_unresolved_model_fails_loud(monkeypatch, seed_catalog):
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.AVG, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Compressor", "Temperature"))
    _stub_sitewise(monkeypatch, models=_SW_MODELS, assets=_SW_ASSETS, values=_SW_VALUES)

    with pytest.raises(MetricResolverError, match="asset model"):
        resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))


def test_sitewise_unresolved_property_fails_loud(monkeypatch, seed_catalog):
    metric = DirectMetric(
        id="m-sw", concept="pressure",
        aggregation=Aggregation.AVG, label="Pressure", unit="bar",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Pressure"))
    _stub_sitewise(monkeypatch, models=_SW_MODELS, assets=_SW_ASSETS, values=_SW_VALUES)

    with pytest.raises(MetricResolverError, match="property"):
        resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))


def test_sitewise_no_usable_values_fails_loud(monkeypatch, seed_catalog):
    """Every asset errors or returns None → fail loud (no silent zero/fallback)."""
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.AVG, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    _stub_sitewise(
        monkeypatch, models=_SW_MODELS, assets=_SW_ASSETS,
        values={
            "a1": {"error": "throttled"},
            "a2": {"value": None, "timestamp": 200},
            "a3": {"error": "not found"},
        },
    )

    with pytest.raises(MetricResolverError, match="no usable SiteWise values"):
        resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))


def test_sitewise_skips_bad_entries_but_aggregates_good(monkeypatch, seed_catalog):
    """Assets with error/None are skipped; the aggregate uses only usable values."""
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.AVG, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    _stub_sitewise(
        monkeypatch, models=_SW_MODELS, assets=_SW_ASSETS,
        values={
            "a1": {"value": 10, "timestamp": 100},
            "a2": {"error": "throttled"},          # skipped
            "a3": {"value": 30, "timestamp": 150},
        },
    )

    resolved = resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))
    assert resolved.value == 20                      # (10 + 30) / 2, a2 skipped


# --- (C) Regression: PostgreSQL behavior unchanged --------------------------

def test_postgresql_path_unchanged(monkeypatch, seed_catalog):
    """A PostgreSQL system still composes + executes the aggregate SELECT via
    query_pg, and never touches the Athena/SiteWise seams."""
    seed_catalog._systems.append({
        "id": "pg-sys", "name": "ERP", "connection_type": ct.POSTGRESQL,
        "schema": "erp_data", "status": "onboarded",
    })
    metric = DirectMetric(
        id="m-pg", concept="order-count",
        aggregation=Aggregation.COUNT, label="Orders", unit="count",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("orders", "id"))
    monkeypatch.setattr(
        mr, "query_pg",
        lambda schema, sql, params=None: json.dumps(
            {"columns": ["count"], "rows": [[7]], "row_count": 1}
        ),
    )

    def _no_touch(*a, **k):
        raise AssertionError("PostgreSQL path must not touch the Athena/SiteWise seams")

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", _no_touch)
    monkeypatch.setattr(mr.sitewise_read, "list_asset_models_with_properties", _no_touch)

    resolved = resolve_metric("m-pg", authority=_FakeAuthority("pg-sys"))
    assert resolved.value == 7
    assert resolved.system_id == "pg-sys"
    assert resolved.lineage.sql == "SELECT COUNT(id), COUNT(id) FROM erp_data.orders"


# --- (D) Cross-account connection_config resolution (secret merge) ----------
# The APP catalog carries only the NON-SECRET snapshot (no external_id, maybe no
# role_arn). For a cross-account SiteWise/Athena system the resolver MUST merge in
# the agent-config secret (role_arn + external_id) so boto3 assumes the correct
# account — mirroring runtime_app. Secret fetch is stubbed; NO live AWS.

def _xacct_catalog(seed_catalog, *, conn_cfg, secret_arn=None):
    """Replace the seeded sitewise-sys entry's connection_config / secret_arn."""
    for s in seed_catalog._systems:
        if s["id"] == "sitewise-sys":
            s["connection_config"] = conn_cfg
            if secret_arn is not None:
                s["secret_arn"] = secret_arn
            return s
    raise AssertionError("sitewise-sys not in seeded catalog")


def _capture_sitewise_cfg(monkeypatch):
    """Stub sitewise_read helpers, capturing the connection_config the resolver
    passes to list_asset_models_with_properties. Returns the captured dict."""
    captured: dict = {}

    def _list_models(connection_config, region=None, **k):
        captured["cfg"] = connection_config
        captured["region"] = region
        return _SW_MODELS

    monkeypatch.setattr(mr.sitewise_read, "list_asset_models_with_properties", _list_models)
    monkeypatch.setattr(
        mr.sitewise_read, "list_assets",
        lambda connection_config, region=None, asset_model_id="", **k: _SW_ASSETS,
    )
    monkeypatch.setattr(
        mr.sitewise_read, "batch_get_property_values",
        lambda connection_config, region, entries: {
            f"{e['assetId']}_{e['propertyId']}": _SW_VALUES[e["assetId"]]
            for e in entries
        },
    )
    return captured


def test_sitewise_merges_secret_when_snapshot_lacks_xacct_creds(monkeypatch, seed_catalog):
    """Catalog snapshot lacks role_arn/external_id but carries a secret_arn ⇒ the
    resolver fetches the agent-config secret and passes a connection_config
    CONTAINING role_arn + external_id (from the secret) to sitewise_read."""
    import agents.system_agent as sa

    _xacct_catalog(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        seed_catalog,
        conn_cfg={"region": "eu-central-1"},          # non-secret snapshot only
        secret_arn="arn:aws:secretsmanager:eu-central-1:111122223333:secret:adt/agent-config/sitewise-sys",
    )
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.AVG, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    captured = _capture_sitewise_cfg(monkeypatch)

    fetched: dict = {}

    def fake_fetch(secret_arn):
        fetched["arn"] = secret_arn
        return {"role_arn": "arn:aws:iam::444455556666:role/lake-read",
                "external_id": "xacct-ext-id", "region": "eu-central-1"}

    monkeypatch.setattr(sa, "_fetch_credentials", fake_fetch)

    resolved = resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))

    # The secret was fetched by ARN and its cross-account creds reached sitewise_read.
    assert fetched["arn"].endswith("agent-config/sitewise-sys")
    assert captured["cfg"]["role_arn"] == "arn:aws:iam::444455556666:role/lake-read"
    assert captured["cfg"]["external_id"] == "xacct-ext-id"
    assert resolved.value == 20                      # (10+20+30)/3, resolution succeeds


def test_sitewise_secret_wins_over_nonsecret_snapshot(monkeypatch, seed_catalog):
    """When both the snapshot and the secret carry a field, the SECRET value wins
    (the snapshot may hold a stale/placeholder role_arn)."""
    import agents.system_agent as sa

    _xacct_catalog(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        seed_catalog,
        conn_cfg={"region": "eu-central-1", "role_arn": "arn:aws:iam::111122223333:role/stale"},
        secret_arn="arn:aws:secretsmanager:eu-central-1:111122223333:secret:adt/agent-config/sitewise-sys",
    )
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.AVG, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    captured = _capture_sitewise_cfg(monkeypatch)
    monkeypatch.setattr(
        sa, "_fetch_credentials",
        lambda arn: {"role_arn": "arn:aws:iam::444455556666:role/lake-read",
                     "external_id": "xacct-ext-id"},
    )

    resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))

    assert captured["cfg"]["role_arn"] == "arn:aws:iam::444455556666:role/lake-read"
    assert captured["cfg"]["external_id"] == "xacct-ext-id"


def test_sitewise_complete_snapshot_skips_secret_fetch(monkeypatch, seed_catalog):
    """When the snapshot ALREADY carries role_arn + external_id, no secret fetch is
    performed (even if a secret_arn is present) — the snapshot is used verbatim."""
    import agents.system_agent as sa

    _xacct_catalog(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        seed_catalog,
        conn_cfg={"region": "eu-central-1",
                  "role_arn": "arn:aws:iam::444455556666:role/lake-read",
                  "external_id": "xacct-ext-id"},
        secret_arn="arn:aws:secretsmanager:eu-central-1:111122223333:secret:adt/agent-config/sitewise-sys",
    )
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.AVG, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    captured = _capture_sitewise_cfg(monkeypatch)

    def _no_fetch(arn):
        raise AssertionError("complete snapshot must NOT trigger a secret fetch")

    monkeypatch.setattr(sa, "_fetch_credentials", _no_fetch)

    resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))

    assert captured["cfg"]["role_arn"] == "arn:aws:iam::444455556666:role/lake-read"
    assert captured["cfg"]["external_id"] == "xacct-ext-id"


def test_sitewise_no_secret_arn_uses_conventional_secret_name(monkeypatch, seed_catalog):
    """No secret_arn on the entry ⇒ the resolver fetches the per-system connection
    secret by its DETERMINISTIC conventional name
    (``adt/agent-config/<system_id>``) and merges the cross-account
    creds. The app catalog snapshot routinely omits BOTH the secret fields and the
    secret_arn pointer, so the convention is what makes cross-account resolution
    work (regression: this used to fall back to ambient creds → wrong account)."""
    import agents.system_agent as sa

    entry = _xacct_catalog(seed_catalog, conn_cfg={"region": "us-east-1"})
    entry.pop("secret_arn", None)
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.MAX, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    captured = _capture_sitewise_cfg(monkeypatch)

    fetched: dict = {}

    def fake_fetch(secret_id):
        fetched["id"] = secret_id
        return {"role_arn": "arn:aws:iam::444455556666:role/lake-read",
                "external_id": "xacct-ext-id"}

    monkeypatch.setattr(sa, "_fetch_credentials", fake_fetch)

    resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))

    # Fetched by the deterministic conventional name, and creds reached sitewise_read.
    assert fetched["id"] == "adt/agent-config/sitewise-sys"
    assert captured["cfg"]["role_arn"] == "arn:aws:iam::444455556666:role/lake-read"
    assert captured["cfg"]["external_id"] == "xacct-ext-id"


def test_sitewise_secret_fetch_failure_is_best_effort(monkeypatch, seed_catalog):
    """A secret-fetch failure is swallowed (best-effort): the resolver falls back
    to the non-secret snapshot rather than raising in _full_connection_config —
    the caller still fails loud downstream if the config is unusable."""
    import agents.system_agent as sa

    _xacct_catalog(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        seed_catalog,
        conn_cfg={"region": "eu-central-1"},
        secret_arn="arn:aws:secretsmanager:eu-central-1:111122223333:secret:adt/agent-config/sitewise-sys",
    )
    metric = DirectMetric(
        id="m-sw", concept="temperature",
        aggregation=Aggregation.AVG, label="Temp", unit="C",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Pump", "Temperature"))
    captured = _capture_sitewise_cfg(monkeypatch)

    def boom(arn):
        raise RuntimeError("AccessDeniedException")

    monkeypatch.setattr(sa, "_fetch_credentials", boom)

    resolved = resolve_metric("m-sw", authority=_FakeAuthority("sitewise-sys"))

    # Fetch failed → snapshot used as-is (no xacct creds merged), no raise here.
    assert captured["cfg"] == {"region": "eu-central-1"}
    assert resolved.value == 20


def test_athena_merges_secret_when_snapshot_lacks_xacct_creds(monkeypatch, seed_catalog):
    """Athena branch also resolves the FULL config: a cross-account Athena system
    whose snapshot lacks role_arn/external_id gets them merged from the secret and
    passed to run_athena_query."""
    import agents.system_agent as sa

    for s in seed_catalog._systems:
        if s["id"] == "athena-sys":
            s["connection_config"] = {"workgroup": "wg", "database": "analytics_db"}
            s["secret_arn"] = "arn:aws:secretsmanager:eu-central-1:111122223333:secret:adt/agent-config/athena-sys"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
            break
    metric = DirectMetric(
        id="m-athena", concept="widget-count",
        aggregation=Aggregation.SUM, label="Widgets", unit="units",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("widgets", "qty"))

    captured: dict = {}

    def fake_run(sql, connection_config, **kwargs):
        captured["cfg"] = connection_config
        return {"columns": ["_col0"], "rows": [["42"]], "row_count": 1}

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", fake_run)
    monkeypatch.setattr(
        sa, "_fetch_credentials",
        lambda arn: {"role_arn": "arn:aws:iam::444455556666:role/lake-read",
                     "external_id": "xacct-ext-id"},
    )

    resolved = resolve_metric("m-athena", authority=_FakeAuthority("athena-sys"))

    assert captured["cfg"]["workgroup"] == "wg"             # snapshot preserved
    assert captured["cfg"]["role_arn"] == "arn:aws:iam::444455556666:role/lake-read"
    assert captured["cfg"]["external_id"] == "xacct-ext-id"  # merged from secret
    assert resolved.value == 42


# ===========================================================================
# (C) SiteWise (Direct) METRIC-BY-DIMENSION — the non-SQL grouped path.
#
# SiteWise has no SQL/join engine, so the grouped resolver branches to
# metric_dimension._resolve_by_dimension_sitewise BEFORE composing SQL. Grouping
# is supported ONLY for the co-located shape (measure + dimension property on the
# SAME asset model): per asset it reads the current dimension value (group key)
# and the current measure value, buckets by group, and reduces each bucket with
# the shared _aggregate_sitewise. A COUNT over a categorical property is the
# machine-state distribution (count of assets in each state). Fully offline —
# the sitewise_read seam + _full_connection_config are stubbed (NO AWS).
# ===========================================================================

from agents import metric_dimension as md  # noqa: E402
from agents.metric_resolver import resolve_metric_by_dimension  # noqa: E402


def _md_neptune_query(mapping_by_concept):
    """neptune_query double for the grouped SiteWise path: _resolve_table_column
    (the only cypher used, both for the measure concept and the dimension concept)
    returns the single co-located mapping row keyed by params['concept']."""
    def nq(cypher, params=None):
        if "se.name AS table" in cypher:
            row = mapping_by_concept.get((params or {}).get("concept"))
            return [row] if row else []
        return []
    return nq


def _stub_sitewise_props(monkeypatch, *, models, assets, values):
    """Stub the sitewise_read seam where per-(asset, property) values back the
    BATCH read (event-loop fix: the grouped sweep calls
    ``batch_get_property_values``), so a measure and a dimension property on the
    same model can return different per-asset values."""
    monkeypatch.setattr(
        mr.sitewise_read, "list_asset_models_with_properties",
        lambda connection_config, region=None, **k: models,
    )
    monkeypatch.setattr(
        mr.sitewise_read, "list_assets",
        lambda connection_config, region=None, asset_model_id="", **k: assets,
    )
    monkeypatch.setattr(
        mr.sitewise_read, "batch_get_property_values",
        lambda connection_config, region, entries: {
            f"{e['assetId']}_{e['propertyId']}": values[(e["assetId"], e["propertyId"])]
            for e in entries
        },
    )


# One asset model bearing a categorical state property + a numeric measure.
_SWD_MODELS = [{
    "id": "model-state", "name": "State_Event",
    "properties": [
        {"id": "prop-tostate", "name": "payload_toState"},
        {"id": "prop-amount", "name": "amount_per_5s"},
    ],
}]
_SWD_ASSETS = [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}, {"id": "a4"}]


def test_sitewise_by_dimension_count_distribution(monkeypatch, seed_catalog):
    """machine-state distribution: COUNT of assets per current state — the measure
    concept and the dimension concept resolve to the SAME categorical property, so
    the single read is reused and each group's COUNT is its asset count."""
    metric = DirectMetric(
        id="m-state-dist", concept="machine-state",
        aggregation=Aggregation.COUNT, label="Machines by State", unit="machines",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {"region": "us-east-1"})
    monkeypatch.setattr(mr, "neptune_query", _md_neptune_query(
        {"machine-state": {"table": "State_Event", "column": "payload_toState"}}
    ))
    # a1,a2 → RUNNING ; a3 → IDLE ; a4 → DOWN
    vals = {
        ("a1", "prop-tostate"): {"value": "RUNNING", "timestamp": 100},
        ("a2", "prop-tostate"): {"value": "RUNNING", "timestamp": 110},
        ("a3", "prop-tostate"): {"value": "IDLE", "timestamp": 120},
        ("a4", "prop-tostate"): {"value": "DOWN", "timestamp": 130},
    }
    _stub_sitewise_props(monkeypatch, models=_SWD_MODELS, assets=_SWD_ASSETS, values=vals)

    result = resolve_metric_by_dimension(
        "m-state-dist", "machine-state", authority=_FakeAuthority("sitewise-sys"),
    )

    assert result.system_id == "sitewise-sys"
    assert result.query_plan.join is None            # SiteWise: never a SQL join
    # ordered by group value (deterministic); COUNT = asset count per state
    assert [(r.dimension_value, r.value) for r in result.rows] == [
        ("DOWN", 1), ("IDLE", 1), ("RUNNING", 2),
    ]
    # per-row lineage carries the SiteWise pseudo-sql + shared plan
    row = result.rows[0]
    assert row.lineage.sql == "sitewise:group_count(State_Event.payload_toState by payload_toState)"
    assert row.lineage.measure_mapping.column == "payload_toState"
    assert row.lineage.dimension_mapping.column == "payload_toState"


def test_sitewise_by_dimension_sum_grouped_distinct_properties(monkeypatch, seed_catalog):
    """SUM of a numeric measure property grouped by a co-located categorical
    property (distinct properties, same model): per group the measure values are
    summed via the shared aggregator."""
    metric = DirectMetric(
        id="m-amount", concept="throughput",
        aggregation=Aggregation.SUM, label="Output", unit="units",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {"region": "us-east-1"})
    monkeypatch.setattr(mr, "neptune_query", _md_neptune_query({
        "throughput": {"table": "State_Event", "column": "amount_per_5s"},
        "machine-state": {"table": "State_Event", "column": "payload_toState"},
    }))
    vals = {
        ("a1", "prop-tostate"): {"value": "RUNNING", "timestamp": 100},
        ("a1", "prop-amount"): {"value": 5, "timestamp": 100},
        ("a2", "prop-tostate"): {"value": "RUNNING", "timestamp": 110},
        ("a2", "prop-amount"): {"value": 7, "timestamp": 110},
        ("a3", "prop-tostate"): {"value": "IDLE", "timestamp": 120},
        ("a3", "prop-amount"): {"value": 2, "timestamp": 120},
        ("a4", "prop-tostate"): {"value": "DOWN", "timestamp": 130},
        ("a4", "prop-amount"): {"value": 0, "timestamp": 130},
    }
    _stub_sitewise_props(monkeypatch, models=_SWD_MODELS, assets=_SWD_ASSETS, values=vals)

    result = resolve_metric_by_dimension(
        "m-amount", "machine-state", authority=_FakeAuthority("sitewise-sys"),
    )

    assert [(r.dimension_value, r.value) for r in result.rows] == [
        ("DOWN", 0), ("IDLE", 2), ("RUNNING", 12),
    ]


def test_sitewise_by_dimension_cross_model_fails_loud(monkeypatch, seed_catalog):
    """SiteWise cannot join across asset models: a measure property and a dimension
    property on DIFFERENT models fail loud (no silent cross-model join)."""
    metric = DirectMetric(
        id="m-amount", concept="throughput",
        aggregation=Aggregation.SUM, label="Output", unit="units",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {"region": "us-east-1"})
    monkeypatch.setattr(mr, "neptune_query", _md_neptune_query({
        "throughput": {"table": "Metric_Event", "column": "amount_per_5s"},
        "machine-state": {"table": "State_Event", "column": "payload_toState"},
    }))

    with pytest.raises(MetricResolverError, match="same asset model"):
        resolve_metric_by_dimension(
            "m-amount", "machine-state", authority=_FakeAuthority("sitewise-sys"),
        )


def test_sitewise_cross_model_error_is_structural(monkeypatch, seed_catalog):
    """The same-model constraint is a DATA-MODEL limit, not a transient fault:
    it must raise StructuralMetricError so the route's envelope carries
    structural: True and the UI drops the Retry affordance (observed live: a
    Retry button on this exact error invited operators to rerun a dead end)."""
    metric = DirectMetric(
        id="m-amount", concept="throughput",
        aggregation=Aggregation.SUM, label="Output", unit="units",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {"region": "us-east-1"})
    monkeypatch.setattr(mr, "neptune_query", _md_neptune_query({
        "throughput": {"table": "Metric_Event", "column": "amount_per_5s"},
        "machine-state": {"table": "State_Event", "column": "payload_toState"},
    }))

    with pytest.raises(mr.StructuralMetricError):
        resolve_metric_by_dimension(
            "m-amount", "machine-state", authority=_FakeAuthority("sitewise-sys"),
        )
    # The envelope builder marks it (key present only when structural).
    env = mr._grouped_error("machine-state", "boom", structural=True)
    assert env["structural"] is True
    assert "structural" not in mr._grouped_error("machine-state", "boom")


def test_sitewise_by_dimension_no_usable_values_fails_loud(monkeypatch, seed_catalog):
    """No asset yields a usable dimension value → fail loud (no silent empty
    distribution)."""
    metric = DirectMetric(
        id="m-state-dist", concept="machine-state",
        aggregation=Aggregation.COUNT, label="Machines by State", unit="machines",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {"region": "us-east-1"})
    monkeypatch.setattr(mr, "neptune_query", _md_neptune_query(
        {"machine-state": {"table": "State_Event", "column": "payload_toState"}}
    ))
    vals = {(a["id"], "prop-tostate"): {"error": "no value"} for a in _SWD_ASSETS}
    _stub_sitewise_props(monkeypatch, models=_SWD_MODELS, assets=_SWD_ASSETS, values=vals)

    with pytest.raises(MetricResolverError, match="no usable SiteWise values"):
        resolve_metric_by_dimension(
            "m-state-dist", "machine-state", authority=_FakeAuthority("sitewise-sys"),
        )
