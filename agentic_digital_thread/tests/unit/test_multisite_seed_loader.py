# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Focused contracts for the ERP multi-site sibling-module loader."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation import seed_data  # noqa: E402

_SEED_FUNCTIONS = (
    "seed_products",
    "seed_inventory_locations",
    "seed_multisite_materials",
    "seed_multisite_finished_goods",
    "seed_sites",
    "seed_product_site_routing",
    "seed_production_lines",
    "seed_site_product_config",
    "seed_multisite_bom",
    "seed_multisite_raw_inventory",
    "seed_multisite_fg_inventory",
    "seed_simulation_config",
    "seed_item_suppliers",
    "seed_material_lots_all_sites",
    "seed_additional_products",
    "seed_machining_site",  # fourth simulated site (ADR-0083)
    "seed_mes_equipment",   # the MES equipment registry the site views read
)


class _Cursor:
    def __init__(self):
        self.events = []
        self.closed = False

    def execute(self, sql):
        self.events.append(("execute", sql))

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self):
        self.cursor_instance = _Cursor()
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _Database:
    def __init__(self):
        self.conn = _Connection()

    def connection(self):
        conn = self.conn

        class _Context:
            def __enter__(self):
                return conn

            def __exit__(self, exc_type, exc, tb):
                return False

        return _Context()


def _handler_path(tmp_path: Path, monkeypatch, source: str) -> Path:
    package_dir = tmp_path / "erp_simulation"
    package_dir.mkdir()
    fake_seed_file = package_dir / "seed_data.py"
    fake_seed_file.write_text("# loader path anchor\n", encoding="utf-8")
    handler = tmp_path / "lambda-db-init" / "handler.py"
    handler.parent.mkdir()
    handler.write_text(source, encoding="utf-8")
    monkeypatch.setattr(seed_data, "__file__", str(fake_seed_file))
    return handler


def _successful_handler_source() -> str:
    names = repr(_SEED_FUNCTIONS)
    return f'''\
import sys

import boto3

def get_schema_sql_part5():
    return "SCHEMA"

def get_views_sql():
    return "VIEWS"

def get_machining_views_sql():
    return "MACHINING_VIEWS"

def seed_suppliers(cursor):
    # The real handler imports its driver (pgdriver) lazily, inside the
    # connect helper, so loading it needs no driver module.
    cursor.events.append(("module", __name__, __file__, boto3, sys.modules.get("pgdriver")))

def _seed(cursor):
    return None

for _name in {names}:
    globals()[_name] = _seed
'''


def test_multisite_loader_uses_db_handler_identity_and_restores_modules(
        tmp_path, monkeypatch):
    expected_handler = _handler_path(
        tmp_path, monkeypatch, _successful_handler_source())
    existing_boto3 = types.ModuleType("boto3")
    monkeypatch.setitem(sys.modules, "boto3", existing_boto3)
    monkeypatch.delitem(sys.modules, "pgdriver", raising=False)
    database = _Database()

    assert seed_data._run_multisite_seed(cast(Any, database)) == 1

    module_event = next(
        event for event in database.conn.cursor_instance.events
        if event[0] == "module")
    assert module_event[1] == "db_handler"
    assert os.path.normpath(module_event[2]) == os.path.normpath(str(expected_handler))
    assert module_event[3] is existing_boto3
    # No stand-in driver module: a stub ``pgdriver`` would shadow the real
    # one the handler's lazy import falls back to.
    assert module_event[4] is None
    assert sys.modules["boto3"] is existing_boto3
    assert "pgdriver" not in sys.modules
    assert database.conn.committed
    assert not database.conn.rolled_back
    assert database.conn.cursor_instance.closed


def test_multisite_seed_seeds_the_equipment_registry_after_the_sites_and_before_the_views(
        tmp_path, monkeypatch):
    """The site MES views read ``mes_equipment``: the loader seeds it after
    the machining site and before either view batch, in the same commit."""
    source = _successful_handler_source() + '''
def _recorder(name):
    def seed(cursor):
        cursor.events.append(("seed", name))
    return seed

for _name in ("seed_machining_site", "seed_mes_equipment"):
    globals()[_name] = _recorder(_name)
'''
    _handler_path(tmp_path, monkeypatch, source)
    monkeypatch.setitem(sys.modules, "boto3", types.ModuleType("boto3"))
    database = _Database()

    assert seed_data._run_multisite_seed(cast(Any, database)) == 1

    events = [e for e in database.conn.cursor_instance.events if e[0] in ("seed", "execute")]
    assert events[-4:] == [("seed", "seed_machining_site"), ("seed", "seed_mes_equipment"),
                           ("execute", "VIEWS"), ("execute", "MACHINING_VIEWS")]
    assert database.conn.committed


@pytest.mark.parametrize("missing", ["spec", "loader"])
def test_multisite_loader_fails_clearly_when_spec_or_loader_is_missing(
        tmp_path, monkeypatch, missing):
    handler = _handler_path(tmp_path, monkeypatch, "# not executed\n")
    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    result = None if missing == "spec" else importlib.machinery.ModuleSpec(
        "db_handler", loader=None)
    monkeypatch.setattr(importlib.util, "spec_from_file_location",
                        lambda name, path: result)

    with pytest.raises(ImportError, match="module spec or loader unavailable"):
        seed_data._run_multisite_seed(cast(Any, _Database()))

    assert handler.exists()
    assert "boto3" not in sys.modules


def test_multisite_loader_propagates_handler_exception_and_restores_modules(
        tmp_path, monkeypatch):
    _handler_path(
        tmp_path, monkeypatch,
        "import boto3\nraise RuntimeError('handler load failed')\n")
    monkeypatch.delitem(sys.modules, "boto3", raising=False)

    with pytest.raises(RuntimeError, match="handler load failed"):
        seed_data._run_multisite_seed(cast(Any, _Database()))

    assert "boto3" not in sys.modules


def test_multisite_loader_keeps_an_existing_boto3_when_the_handler_fails(
        tmp_path, monkeypatch):
    _handler_path(
        tmp_path, monkeypatch,
        "import boto3\nraise RuntimeError('handler load failed')\n")
    existing_boto3 = types.ModuleType("boto3")
    monkeypatch.setitem(sys.modules, "boto3", existing_boto3)

    with pytest.raises(RuntimeError, match="handler load failed"):
        seed_data._run_multisite_seed(cast(Any, _Database()))

    assert sys.modules["boto3"] is existing_boto3
