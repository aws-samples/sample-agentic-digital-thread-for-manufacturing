# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Segment map parity: db-init's customer_product_categories view and the
order simulator must agree on segment -> category.

The db-init Lambda never imports ``erp_simulation``, so it carries its own
``SEGMENT_CATEGORIES`` literal. It is read here with ``ast.literal_eval`` (no
import of the Lambda module, no boto3 or database driver needed) and compared as sets.
"""
from __future__ import annotations

import ast
from pathlib import Path

from erp_simulation import segments

_HANDLER = Path(__file__).resolve().parents[2] / "lambda-db-init" / "handler.py"


def _db_init_segment_categories() -> dict:
    tree = ast.parse(_HANDLER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SEGMENT_CATEGORIES" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"SEGMENT_CATEGORIES literal not found in {_HANDLER}")


def test_db_init_segment_categories_match_simulator():
    db_init = _db_init_segment_categories()
    assert {k: set(v) for k, v in db_init.items()} == {
        k: set(v) for k, v in segments.SEGMENT_CATEGORIES.items()
    }


def test_db_init_segment_lists_have_no_duplicates():
    # Set comparison would hide a duplicated category in the db-init list.
    for segment, categories in _db_init_segment_categories().items():
        assert len(categories) == len(set(categories)), segment
