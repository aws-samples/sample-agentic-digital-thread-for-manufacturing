#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
seed_local_db.py — seed the ERP/MES view-schemas into the local Postgres.

The ADT catalog points `erp`/`mes-mil`/`mes-det`/`mes-cr` at the view-schemas
`erp_data`, `mes_milwaukee`, `mes_detroit`, `mes_cedarrapids`. In a deployment
those are created by `lambda-db-init/handler.py` in the ERP database (views over
`public.*`, site-filtered for MES). `lambda-adt-seed/*.sql` only provides
`cmms_maintenance` + `plm_engineering` (auto-seeded into the local DB on first
`up`). This script runs `lambda-db-init`'s create/seed logic against the SINGLE
local Postgres so every catalog system is onboardable locally.

`drop_all_tables()` also DROPs the four view-schemas CASCADE first, clearing the
stale raw `mes_detroit`/`erp_detroit` schemas that `lambda-adt-seed` created.

Usage (env: POSTGRES_HOST/PORT/DB/USER/PASSWORD, as in .env.local):
    .venv-local/bin/python scripts/seed_local_db.py
"""
import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from erp_simulation import pgdriver  # noqa: E402 - needs the repository root on sys.path

HANDLER_PATH = os.path.join(os.path.dirname(__file__), "..", "lambda-db-init", "handler.py")


def _load_handler():
    spec = importlib.util.spec_from_file_location("dbinit_handler", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _connect():
    pw = os.environ.get("POSTGRES_PASSWORD")
    if not pw:
        print("POSTGRES_PASSWORD not set — source .env.local first.", file=sys.stderr)
        sys.exit(2)
    return pgdriver.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "adt_demo"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=pw,
        connect_timeout=10,
    )


def main() -> int:
    # drop_all_tables terminates other connections to DB_NAME — point it at our DB.
    os.environ.setdefault("DB_NAME", os.environ.get("POSTGRES_DB", "adt_demo"))
    h = _load_handler()
    conn = _connect()
    try:
        print("==> drop_all_tables (clears stale view-schemas + public tables)")
        h.drop_all_tables(conn)
        print("==> create_schema (public tables + erp_data/mes_* views)")
        h.create_schema(conn)
        print("==> seed_data (reference + simulation data)")
        h.seed_data(conn)
        ok = h.verify_schema(conn)
        print(f"==> verify_schema -> {ok}")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
