# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""The Lambda layer's PostgreSQL client module, and test DDL/DML composed with
its ``sql`` module.

Identifiers (database, role, schema, table and column names) are quoted by the
driver instead of being interpolated into the statement text, which is also
what the application's own PostgreSQL writers do.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

#: The layer's copy of pgdriver.py (one of the identical copies,
#: tests/test_pgdriver_copies.py), importable as ``pgdriver`` the way the
#: functions on the layer import it.
LAYER_DIR = Path(__file__).resolve().parents[1] / "lambda-layers" / "pg8000"


_MODULE = None


def pgdriver():
    """The ``pgdriver`` module the layer's functions import (the layer
    directory goes on ``sys.path`` once). Imported lazily: it needs pg8000.
    The real module even while a test has a stand-in at
    ``sys.modules["pgdriver"]``."""
    global _MODULE
    if _MODULE is None:
        if str(LAYER_DIR) not in sys.path:
            sys.path.insert(0, str(LAYER_DIR))
        loaded = sys.modules.get("pgdriver")
        if isinstance(loaded, types.ModuleType) and getattr(loaded, "__file__", None):
            _MODULE = loaded
        else:
            stand_in = sys.modules.pop("pgdriver", None)
            try:
                import pgdriver as module
            finally:
                if stand_in is not None:
                    sys.modules["pgdriver"] = stand_in
            _MODULE = module
    return _MODULE


def stmt(template: str, /, **pieces):
    """Render *template* (``{name}`` placeholders, ``%s`` left for bound params).

    Each keyword is an identifier (``str``), a dotted identifier (``tuple`` of
    ``str``) or an already-composed ``pgdriver.sql.Composable`` (for example
    ``sql.Literal(...)``), which passes through unchanged.
    """
    sql = pgdriver().sql

    def piece(value):
        if isinstance(value, sql.Composable):
            return value
        if isinstance(value, tuple):
            return sql.Identifier(*value)
        return sql.Identifier(value)

    return sql.SQL(template).format(**{name: piece(value) for name, value in pieces.items()})
