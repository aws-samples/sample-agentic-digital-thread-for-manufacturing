# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin ``/migrate/erp-mes-views`` DDL (T9): characterization + db-init parity.

The characterization digests below were captured from HEAD's hand-written
``view_sql`` literal in ``admin_routes.migrate_erp_mes_views`` BEFORE the DDL
moved to ``agents.demo.site_views``: each is ``sha256(stmt)[:16]`` of one
``;``-separated statement with whitespace collapsed to single spaces. Digests
(not text) keep demo schema/site literals out of this engine-scanned test; the
generated set must stay a SUPERSET, so no legacy statement can silently drift.
Expected names are otherwise derived from ``site_views`` constants
(imported per test so the characterization tests also run against HEAD).
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from agents import admin_routes

_REPO_ROOT = Path(__file__).resolve().parents[3]

# 42 statements of HEAD's admin literal (4 CREATE SCHEMA + 38 CREATE VIEW).
_HEAD_ADMIN_STATEMENT_DIGESTS = frozenset({
    "20fccd0c45c40331", "03e1a0cbe3bdc325", "38c56e534aa0d0c1", "70abe80fdccd5b85",
    "e2baeb84afe76264", "a9686f899572c37b", "c3aae393a20290d6", "d628dc121287b4f6",
    "c4e4e1a533468c1f", "59398a610c6f80ec", "99ca6a89f9e361e4", "83c855beabed138a",
    "192515d59dbc863f", "14df61443f992451", "c3e5814068d61ee8", "110efbadc0aa5225",
    "17e734e26deed534", "d6411752d1dbe33b", "c594934ffc7c67b0", "7c17e8c93c15c1b1",
    "5f923af586a286f9", "9f2f3a6b5e86f0eb", "64a80936d7303a08", "460efae910c49961",
    "0cbca9b18b85edb5", "989dda4dfd87b5f6", "b94ba6ce048ace2a", "2e672ad630b9e184",
    "39a99fae6a43343f", "b460b41effe11723", "a1042eab497db30a", "b55e221f0656bbc0",
    "8bda5a26269c99e9", "c337b46d8e7c0b6f", "9f9558c3257df0c1", "84cf8d6855341d14",
    "f6d2e6e2366fb5f4", "80faafbe7b20c0bb", "23a47f1d0d326590", "c7c52ebdd8653cbc",
    "e1cab7ae502bbd8f", "a67998ff36b20490",
})

_VIEW_NAME = re.compile(r"^CREATE OR REPLACE VIEW (\w+\.\w+) ")


def _statements(sql: str) -> list[str]:
    return [" ".join(s.split()) for s in sql.split(";") if s.strip()]


def _digest(statement: str) -> str:
    return hashlib.sha256(statement.encode()).hexdigest()[:16]


def _qualified_views(sql: str) -> set[str]:
    """Schema-qualified view names (``schema.view``) created by ``sql``."""
    return {m.group(1) for s in _statements(sql) if (m := _VIEW_NAME.match(s))}


class _FakeCursor:
    def __init__(self, log):
        self._log = log

    def execute(self, sql):
        self._log.append(sql)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, log):
        self._log = log
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._log)

    def close(self):
        self.closed = True


def _request(is_admin: bool):
    return types.SimpleNamespace(state=types.SimpleNamespace(is_admin=is_admin))


@pytest.fixture
def executed(monkeypatch):
    """Run the admin migration against a fake pgdriver.connect; return executed SQL."""
    from agents import pgdriver
    from agents.demo import db_routing

    log: list[str] = []
    monkeypatch.setattr(db_routing, "demo_pg_dsn", lambda db: f"dsn:{db}")
    monkeypatch.setattr(pgdriver, "connect", lambda dsn: _FakeConn(log))
    result = admin_routes.migrate_erp_mes_views(_request(True))
    return result, log


def test_migration_requires_admin():
    with pytest.raises(HTTPException) as exc:
        admin_routes.migrate_erp_mes_views(_request(False))
    assert exc.value.status_code == 403


def test_migration_executes_one_batch_and_reports_ok(executed):
    result, log = executed
    assert result == {"status": "ok", "message": "ERP and MES view schemas created in Aurora"}
    assert len(log) == 1


def test_migration_reports_connect_error(monkeypatch):
    from agents import pgdriver
    from agents.demo import db_routing

    def _boom(dsn):
        raise RuntimeError("no route")

    monkeypatch.setattr(db_routing, "demo_pg_dsn", lambda db: "dsn")
    monkeypatch.setattr(pgdriver, "connect", _boom)
    assert admin_routes.migrate_erp_mes_views(_request(True)) == {
        "status": "error", "error": "migration failed", "error_type": "RuntimeError"}


def test_migration_failure_closes_the_connection_and_logs_masked(monkeypatch, caplog):
    """A DDL failure: connection closed in ``finally``; the client gets a
    generic error + class name; the server log carries the digit-masked message."""
    from agents import pgdriver
    from agents.demo import db_routing

    conn = _FakeConn([])

    def _fail(sql):
        raise ValueError("relation public.t42 missing at 10.0.0.7")

    conn.cursor = lambda: types.SimpleNamespace(execute=_fail, close=lambda: None)
    monkeypatch.setattr(db_routing, "demo_pg_dsn", lambda db: "dsn")
    monkeypatch.setattr(pgdriver, "connect", lambda dsn: conn)
    with caplog.at_level("WARNING", logger=admin_routes.logger.name):
        result = admin_routes.migrate_erp_mes_views(_request(True))
    assert result == {"status": "error", "error": "migration failed", "error_type": "ValueError"}
    assert conn.closed
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "relation public.t## missing at ##.#.#.#" in logged and "10.0.0.7" not in logged


def test_migration_success_closes_the_connection(monkeypatch):
    from agents import pgdriver
    from agents.demo import db_routing

    conn = _FakeConn([])
    monkeypatch.setattr(db_routing, "demo_pg_dsn", lambda db: "dsn")
    monkeypatch.setattr(pgdriver, "connect", lambda dsn: conn)
    assert admin_routes.migrate_erp_mes_views(_request(True))["status"] == "ok"
    assert conn.closed and conn.autocommit is True


@pytest.mark.parametrize("attr, bad", [
    ("ERP_DATA_VIEWS", ("customers; DROP TABLE x",)),
    ("ERP_DATA_VIEWS", ("Customers",)),
    ("SITE_MES_VIEWS", (("mes-x", "SITE_A", "SA"),)),
    ("SITE_MES_VIEWS", (("mes_x", "site'a", "SA"),)),
    ("SITE_MES_VIEWS", (("mes_x", "SITE_A", "S A"),)),
])
def test_site_views_refuse_unsafe_identifiers_and_literals(monkeypatch, attr, bad):
    from agents.demo import site_views

    monkeypatch.setattr(site_views, attr, bad)
    with pytest.raises(ValueError, match="unsafe"):
        site_views.erp_mes_views_sql()


def test_executed_ddl_is_a_superset_of_head_admin_literal(executed):
    _, log = executed
    digests = {_digest(s) for s in _statements(log[0])}
    assert _HEAD_ADMIN_STATEMENT_DIGESTS <= digests


def test_executed_ddl_is_site_views_output(executed):
    from agents.demo import site_views

    _, log = executed
    assert log[0] == site_views.erp_mes_views_sql()


def test_site_views_cover_every_declared_site_and_erp_table():
    from agents.demo import site_views

    views = _qualified_views(site_views.erp_mes_views_sql())
    expected = {f"erp_data.{t}" for t in site_views.ERP_DATA_VIEWS}
    for schema, _site, _prefix in site_views.SITE_MES_VIEWS:
        expected |= {f"{schema}.{v}" for v in site_views.SITE_MES_VIEW_NAMES}
    assert views == expected
    assert len(site_views.SITE_MES_VIEWS) == 4


def _load_db_init_handler():
    """lambda-db-init has a dash in the directory name — load it by path."""
    path = _REPO_ROOT / "lambda-db-init" / "handler.py"
    spec = importlib.util.spec_from_file_location("adt_db_init_handler_t9", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_site_views_match_db_init_views():
    """Same schema-qualified views AND statements as db-init creates.

    db-init's unqualified ``customer_product_categories`` (public schema) is
    not an onboarding view schema and stays db-init-only.
    """
    from agents.demo import site_views

    handler = _load_db_init_handler()
    db_init_sql = handler.get_views_sql() + handler.get_machining_views_sql()
    ours = site_views.erp_mes_views_sql()
    assert _qualified_views(ours) == _qualified_views(db_init_sql)
    qualified = [s for s in _statements(db_init_sql)
                 if _VIEW_NAME.match(s) or s.startswith("CREATE SCHEMA")]
    assert set(_statements(ours)) == set(qualified)


def _view(sql: str, qualified: str) -> str:
    matches = [s for s in _statements(sql)
               if s.startswith(f"CREATE OR REPLACE VIEW {qualified} AS ")]
    assert len(matches) == 1, qualified
    return matches[0]


def _site_mes_views():
    from agents.demo import site_views
    return site_views.SITE_MES_VIEWS


@pytest.mark.parametrize("schema, site_code, _prefix", _site_mes_views())
def test_every_site_mes_view_schema_exposes_its_production_runs(schema, site_code, _prefix):
    """``production_runs`` is the ERP production order the MES pulls: the run
    id UNDER the work order's column name (so provision's b3 ``*_id``
    heuristic joins it to ``work_orders.production_run_id``), the run number
    the ERP ships under, then the part (``products.sku``, no product UUID),
    planned quantity, status, actual times and line — APPENDED, so
    ``CREATE OR REPLACE VIEW`` still succeeds over the original 2-column
    view — and only the runs THIS site's work orders execute (D18, ADR-0087)."""
    from agents.demo import site_views
    assert "production_runs" in site_views.SITE_MES_VIEW_NAMES
    stmt = _view(site_views.erp_mes_views_sql(), f"{schema}.production_runs")
    select = stmt.split(" FROM ", 1)[0]
    cols = [c.strip().rsplit(" ", 1)[-1].rsplit(".", 1)[-1]
            for c in select.split(" AS SELECT ", 1)[1].split(",")]
    assert cols[:2] == ["production_run_id", "run_number"]
    assert cols == ["production_run_id", "run_number", "sku", "planned_quantity", "status",
                    "actual_start_time", "actual_end_time", "line"]
    assert "product_id" not in select
    assert "FROM public.production_runs pr JOIN public.products p ON p.product_id = pr.product_id" in stmt
    assert re.search(r"FROM public\.mes_work_orders \w+ WHERE \w+\.site = '"
                     + re.escape(site_code) + "'", stmt)


def test_frozen_mes_seed_has_no_run_number():
    """The frozen story's plant MES seed (lambda-adt-seed 003) carries no
    ``run_number`` column, so the live-story hop never reaches it."""
    sql = (_REPO_ROOT / "lambda-adt-seed" / "003_mes_line3.sql").read_text(encoding="utf-8")
    assert not re.search(r"\brun_number\b", sql, re.IGNORECASE)


@pytest.mark.parametrize("schema, site_code, _prefix", _site_mes_views())
def test_every_site_mes_view_schema_exposes_its_equipment_registry(schema, site_code, _prefix):
    """``equipment`` is the site's MES equipment registry: the line, then the
    machine's id, name, type and status, only THIS site's rows, and no site
    column (the schema is the site)."""
    from agents.demo import site_views
    assert "equipment" in site_views.SITE_MES_VIEW_NAMES
    stmt = _view(site_views.erp_mes_views_sql(), f"{schema}.equipment")
    assert stmt == (f"CREATE OR REPLACE VIEW {schema}.equipment AS SELECT line, equipment_id, "
                    f"equipment_name, equipment_type, status FROM public.mes_equipment "
                    f"WHERE site = '{site_code}'")


def test_every_curated_mes_schema_maps_the_equipment_view_to_the_asset_entity():
    """A curated MES provision resolves the new view to an entity instead of
    tripping the fail-loud totality (``concept_tagging.entity_for_table``)."""
    from agents.concept_tagging import ENTITY_MAPPED_SCHEMAS, entity_for_table

    curated = sorted(s for s in ENTITY_MAPPED_SCHEMAS if s.startswith("mes_"))
    assert curated
    for schema in curated:
        assert entity_for_table(schema, "equipment") == "asset", schema
