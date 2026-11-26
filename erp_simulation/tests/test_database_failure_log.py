# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A failed query logs the SQL text, never bound values or driver text that repeats them."""

import contextlib
import logging

import pytest

from erp_simulation.database import DatabaseManager


class _Cursor:
    description = None

    def execute(self, query, params=None):
        raise RuntimeError("invalid input: customer-secret-value")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def cursor(self):
        return _Cursor()

    def rollback(self):
        pass


@pytest.mark.parametrize("method", ["execute_query", "execute_update"])
def test_failure_log_omits_params(monkeypatch, caplog, method):
    manager = DatabaseManager()
    monkeypatch.setattr(manager, "connection", lambda: contextlib.nullcontext(_Conn()))
    with caplog.at_level(logging.ERROR, logger="erp_simulation.database"):
        with pytest.raises(RuntimeError):
            getattr(manager, method)("SELECT 1 WHERE x = %s", ("customer-secret-value",))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "SELECT 1" in text
    assert "customer-secret-value" not in text
