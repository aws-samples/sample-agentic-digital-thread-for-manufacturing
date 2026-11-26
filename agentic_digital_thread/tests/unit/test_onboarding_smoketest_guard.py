# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the fail-loud guard in scripts/onboarding_smoketest.py.

``guard_required_system`` refuses to proceed with a REQUIRED demo system when its
LIVE registration identity is unusable — absent from the catalog, or PostgreSQL
with an empty schema — so the harness fails LOUDLY instead of silently driving
discover with an empty schema. Deterministic + offline (plain dict identities; no
network, no env, no hardcoded manifest).
"""
import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "onboarding_smoketest.py"
_spec = importlib.util.spec_from_file_location("onboarding_smoketest", _HARNESS_PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def test_absent_from_catalog_aborts_with_actionable_message():
    err = smoke.guard_required_system("erp", None)
    assert err  # non-empty => abort
    assert "erp" in err
    # Mentions the deploy must register demo systems / ADT_DEMO.
    assert "ADT_DEMO" in err or "seed" in err.lower()


def test_postgres_empty_schema_aborts():
    err = smoke.guard_required_system(
        "cmms", {"connection_type": "PostgreSQL", "schema": ""})
    assert err
    assert "schema" in err.lower()


def test_postgres_whitespace_only_schema_aborts():
    err = smoke.guard_required_system(
        "plm", {"connection_type": "PostgreSQL", "schema": "   "})
    assert err


def test_happy_path_registered_postgres_proceeds():
    err = smoke.guard_required_system(
        "mes-mil", {"connection_type": "PostgreSQL", "schema": "mes_milwaukee"})
    assert err == ""


def test_non_postgres_empty_schema_proceeds():
    """A non-PostgreSQL system legitimately has no schema — the empty-schema rule
    only applies to PostgreSQL, so it must not be aborted here."""
    err = smoke.guard_required_system(
        "sitewise-direct", {"connection_type": "SiteWise (Direct)", "schema": ""})
    assert err == ""


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
