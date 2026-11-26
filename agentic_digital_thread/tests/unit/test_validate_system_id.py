# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.db.validate_system_id``.

The onboarding commit-tier tools (``store_credentials``/``discover``) use this
validator to guard system_ids that flow into namespace keys like
``adt/systems/{system_id}``. Unlike the strict SQL/Cypher
``validate_identifier``, system_ids are hyphenated in this codebase (e.g.
``mes-mil``, ``digital-thread`` — ``connect_new_system`` generates them via
``name.lower().replace(' ', '-')``), so the validator must ALLOW hyphens and
underscores while still REJECTING namespace-escape inputs (path separators,
``..``, whitespace, empty). Errors propagate — no fallback.

Fully offline.
"""
import pytest

from agents.db import validate_system_id


@pytest.mark.parametrize("system_id", ["mes-mil", "digital-thread", "erp_1", "ERP", "a-b_c-1"])
def test_valid_system_ids_pass(system_id):
    # Returns the value unchanged (no fallback / no mutation).
    assert validate_system_id(system_id) == system_id


@pytest.mark.parametrize(
    "system_id",
    [
        "a/b",       # path separator
        "a\\b",      # windows path separator
        "..",        # parent-dir escape
        "a/../b",    # path-traversal escape
        "",          # empty
        "a b",       # whitespace
        "a.b",       # dot (not an allowed char)
    ],
)
def test_namespace_escape_ids_raise(system_id):
    with pytest.raises(ValueError):
        validate_system_id(system_id)


def test_none_raises():
    with pytest.raises(ValueError):
        validate_system_id(None)  # type: ignore[arg-type]
