# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared PG-lane support for the live-story evidence tests (ADR-0085/0087).

Builds the two databases a workshop account has (ERP: ``lambda-db-init``
``create_schema`` + ``seed_data`` incl. the site MES views; seed:
``lambda-adt-seed`` 001/003/005/011), optionally writes the live-story
history, and routes the ENGINE's own sampling and membership-probe SQL
(``resolve_evidence.sample_field_values`` / ``probe_field_values`` through
``agents.db.pg``) at them by the production ``is_erp_schema`` rule, so the
exact-agreement pass is measured on exactly what onboarding would see.

Not a test module; imported by ``test_story_traceability_pg.py`` and
``test_exact_affinity_pg.py``. Needs ``ERP_SIM_TEST_PG_DSN``.
"""
from __future__ import annotations

import contextlib
import importlib.util
import time
import uuid
from pathlib import Path

from ._pg_sql import stmt

REPO = Path(__file__).resolve().parents[3]
SEED_SQL_NUMBERS = ("001", "003", "005", "011")
ERP_VIEW_SCHEMA = "erp_data"
#: Column types a text identifier can live in (numbers and timestamps never
#: carry the shape the exact-agreement pass rides on).
TEXT_TYPES = ("character varying", "text", "character", "uuid")


def _load_db_init():
    spec = importlib.util.spec_from_file_location(
        "db_init_handler_story_pg_support", REPO / "lambda-db-init" / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(conns: dict) -> None:
    db_init = _load_db_init()
    db_init.create_schema(conns["erp"])
    db_init.seed_data(conns["erp"])
    with conns["seed"].cursor() as cur:
        for number in SEED_SQL_NUMBERS:
            (path,) = (REPO / "lambda-adt-seed").glob(f"{number}_*.sql")
            cur.execute(path.read_text(encoding="utf-8"))
    conns["seed"].commit()


@contextlib.contextmanager
def story_databases(dsn: str, prefix: str, *, with_history: bool = True):
    """Yield ``(conns, plan)``: ``conns`` = {erp|seed: connection}; ``plan`` is
    the written story plan, or None when ``with_history`` is False (frozen)."""
    from agents import pgdriver
    from agents.pgdriver import make_dsn, parse_dsn
    from erp_simulation import story_history
    names = {k: f"{prefix}{k}_{uuid.uuid4().hex[:8]}" for k in ("erp", "seed")}
    admin = pgdriver.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        for name in names.values():
            cur.execute(stmt("CREATE DATABASE {db}", db=name))
    conns = {k: pgdriver.connect(make_dsn(**{**parse_dsn(dsn), "dbname": v})) for k, v in names.items()}
    try:
        _build(conns)
        plan = None
        if with_history:
            plan = story_history.plan(float(int(time.time())))
            story_history.write(conns["erp"], conns["seed"], plan)
        yield conns, plan
    finally:
        for conn in conns.values():
            conn.close()
        with admin.cursor() as cur:
            for name in names.values():
                cur.execute(stmt("DROP DATABASE IF EXISTS {db} WITH (FORCE)", db=name))
        admin.close()


def routed_pg(conns: dict):
    """An ``agents.db.pg`` stand-in over the test databases (search_path = the
    schema, as production), routed by ``db_routing.is_erp_schema``.
    ``statement_timeout_ms`` sets the statement deadline as ``db.pg`` does (for
    this read only); a failed read rolls back and raises."""
    from agents.demo import db_routing

    def pg(schema, sql, params=None, statement_timeout_ms=None):
        conn = conns["erp"] if db_routing.is_erp_schema(schema) else conns["seed"]
        try:
            with conn.cursor() as cur:
                # SET cannot take a bound parameter; set_config can.
                cur.execute("SELECT set_config('search_path', %s, false)", (schema,))
                if statement_timeout_ms is not None:
                    cur.execute("SELECT set_config('statement_timeout', %s, true)",
                                (str(int(statement_timeout_ms)),))
                cur.execute(sql, params)
                rows = [list(r) for r in cur.fetchall()]
        finally:
            conn.rollback()
        return {"rows": rows}
    return pg


def text_columns(pg, schema: str) -> list[tuple[str, str]]:
    """(table, column) of every text-typed, non-measure column of *schema*."""
    from agents.join_discovery import is_measure_like
    rows = pg(schema, "SELECT table_name, column_name, data_type FROM information_schema.columns "
                      "WHERE table_schema = %s ORDER BY table_name, column_name", (schema,))["rows"]
    return [(t, c) for t, c, dt in rows if dt in TEXT_TYPES and not is_measure_like(c)]


def sampled(system_id: str, schema: str, table: str, field: str, concept: str):
    """A SampledField sampled by the ENGINE's own sampler (DISTINCT, LIMIT 200)."""
    from agents.identity_affinity import SampledField
    from agents.resolve_evidence import sample_field_values
    values = sample_field_values(system_id=system_id, connection_type="PostgreSQL",
                                 schema=schema, table=table, field=field)
    return SampledField(system_id, table, field, concept, tuple(str(v) for v in values))


def engine_probe(monkeypatch, schema_of: dict):
    """The membership probe onboarding injects — the REAL seam,
    ``onboard._affinity_probe`` — over a catalog naming each system's schema
    (every system is PostgreSQL here)."""
    from types import SimpleNamespace
    from agents import onboard

    def get(system_id):
        schema = schema_of.get(system_id)
        return {"id": system_id, "schema": schema, "connection_type": "PostgreSQL"} if schema else None
    monkeypatch.setattr(onboard, "catalog", SimpleNamespace(get=get))
    return onboard._affinity_probe()
