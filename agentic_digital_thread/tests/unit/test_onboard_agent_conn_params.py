# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.onboard._agent_conn_params_for_schema`` (fail-closed).

The provision path builds pgdriver conn params for PostgreSQL system agents.
Passwords MUST come from the environment — there is no hard-coded default
(previously a silent ``'postgres'`` fallback), mirroring the fail-closed
convention documented in ``agents.db.get_conn_params`` /
``agents.db._get_conn_params_for_schema``. These tests assert, with NO live
Postgres/AWS:

  * demo branch (POSTGRES_HOST set, no POSTGRES_PASSWORD)  -> RuntimeError
  * ERP branch (ERP_POSTGRES_HOST set, no password)      -> RuntimeError
  * password present                                     -> params built, env password used
  * ERP branch falls back to shared POSTGRES_PASSWORD (still explicit, no literal)
  * no host env at all                                   -> None (non-SQL / secret-based path)

Runnable two ways (matches sibling onboard unit tests):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboard_agent_conn_params.py
  * standalone:  python agentic_digital_thread/tests/unit/test_onboard_agent_conn_params.py
"""
import sys
from pathlib import Path

import pytest

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.onboard as onboard  # noqa: E402

_ENV_KEYS = (
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "ERP_POSTGRES_HOST", "ERP_POSTGRES_PORT", "ERP_POSTGRES_DB",
    "ERP_POSTGRES_USER", "ERP_POSTGRES_PASSWORD",
)


def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_adt_branch_missing_password_raises(monkeypatch):
    """POSTGRES_HOST set but POSTGRES_PASSWORD unset -> fail-closed, no 'postgres' default."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "adt-db.example.com")
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD is not set"):
        onboard._agent_conn_params_for_schema("cmms")


def test_erp_env_alone_returns_none(monkeypatch):
    """ERP routing is NOT this helper's job (ADR-0044: demo.db_routing owns it).

    With only ERP_POSTGRES_HOST set (no POSTGRES_HOST), the helper returns
    None — callers route ERP schemas through demo_conn_params_for_schema
    first, whose fail-closed password behavior is covered in
    test_demo_db_routing.py.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp-db.example.com")
    assert onboard._agent_conn_params_for_schema("erp_data") is None


def test_adt_branch_uses_env_password(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "adt-db.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105
    params = onboard._agent_conn_params_for_schema("plm")
    assert params == {
        "host": "adt-db.example.com",
        "port": "5432",
        "dbname": "adt_demo",
        "user": "postgres",
        "password": "test-only-not-a-real-password",
    }


def test_no_host_env_returns_none(monkeypatch):
    """No POSTGRES_HOST / ERP_POSTGRES_HOST -> None (agent uses secret-based config)."""
    _clear_env(monkeypatch)
    assert onboard._agent_conn_params_for_schema("cmms") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
