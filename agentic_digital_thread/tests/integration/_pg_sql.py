# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Compose test DDL/DML with ``agents.pgdriver``'s ``sql`` classes.

Identifiers (database, role, schema, table and column names) are quoted by the
driver instead of being interpolated into the statement text, which is also
what the application's own PostgreSQL writers do.
"""
from __future__ import annotations


def stmt(template: str, /, **pieces):
    """Render *template* (``{name}`` placeholders, ``%s`` left for bound params).

    Each keyword is an identifier (``str``), a dotted identifier (``tuple`` of
    ``str``) or an already-composed ``pgdriver.sql.Composable`` (for example
    ``sql.Literal(...)``), which passes through unchanged.
    """
    from agents.pgdriver import sql  # lazy: the PG-gated tests import this module even when pg8000 is absent

    def piece(value):
        if isinstance(value, sql.Composable):
            return value
        if isinstance(value, tuple):
            return sql.Identifier(*value)
        return sql.Identifier(value)

    return sql.SQL(template).format(**{name: piece(value) for name, value in pieces.items()})
