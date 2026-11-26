#!/usr/bin/env python
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Idempotent local-Postgres seeder for the S0.2 integration harness.

This is the ONLY writer to the local substrate. It applies the UNCHANGED
repo-root seed files in order against the local demo database so the resolver/
audit value-read lane has real rows to read — credential-free and deterministic.

Run from ``agentic_digital_thread/``::

    python tests/integration/seed_pg.py

Connection params come from ``agents.db.get_conn_params()`` (same defaults the
app uses: localhost/5432, db ``adt_demo``, user ``postgres``; the password is
read from the required ``POSTGRES_PASSWORD`` environment variable — the same
one the integration compose file requires).
The ``cmms_maintenance`` schema routes to the DEFAULT demo database, so the local
substrate is sufficient for the value-read lane.

If no local Postgres is reachable the script prints a skip line and exits 0
(no traceback) — the harness stays usable when Docker is not running.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Resolve layout: this file is agentic_digital_thread/tests/integration/seed_pg.py
# parents[2] == agentic_digital_thread/ (so `import agents.db` works regardless of CWD)
# parents[3] == repo root (where lambda-adt-seed/ lives).
_HERE = Path(__file__).resolve()
_ADT_ROOT = _HERE.parents[2]
_REPO_ROOT = _HERE.parents[3]

if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents import pgdriver  # noqa: E402  (after sys.path setup)
from agents.db import get_conn_params  # noqa: E402  (after sys.path setup)

# Applied IN ORDER. These files are idempotent (IF NOT EXISTS / DO blocks /
# perpetual-freshness rebuilds), so re-running is safe.
SEED_FILES = (
    "001_schemas.sql",
    "004_cmms_maintenance.sql",
    "010_cmms2_synonyms.sql",
)


def _seed_paths() -> list[Path]:
    """Resolve the seed SQL files relative to the repo root; fail fast if missing."""
    seed_dir = _REPO_ROOT / "lambda-adt-seed"
    paths = []
    for name in SEED_FILES:
        path = seed_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"seed file not found: {path}")
        paths.append(path)
    return paths


def _connect():
    """Connect to the local demo Postgres, or return None if unreachable."""
    try:
        conn = pgdriver.connect(connect_timeout=5, **get_conn_params())
        conn.autocommit = True
        return conn
    except pgdriver.OperationalError:
        return None


def _seed_handler():
    """lambda-adt-seed/handler.py, whose apply_seed_files this seeder uses so
    a local run applies the files exactly as the seed Lambda does."""
    spec = importlib.util.spec_from_file_location(
        "adt_seed_handler_local", _REPO_ROOT / "lambda-adt-seed" / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(conn, paths: list[Path]) -> None:
    """Execute each whole seed file in order, as lambda-adt-seed/handler.py
    does (its apply_seed_files): a file that fails does not stop the rest, and
    the run then raises naming every failed file and the first error."""
    cur = conn.cursor()
    try:
        _seed_handler().apply_seed_files(cur, [str(path) for path in paths])
    finally:
        cur.close()


def main() -> int:
    paths = _seed_paths()
    conn = _connect()
    if conn is None:
        print("no local Postgres — skipping")
        return 0
    try:
        _apply(conn, paths)
    finally:
        conn.close()
    print("local Postgres seed complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
