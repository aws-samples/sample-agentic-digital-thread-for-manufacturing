# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The DuckDB setup statements are fixed literals, and best-effort cleanup
failures are logged at debug instead of dropped.

DuckDB cannot bind an extension name or a catalog alias, so the LOAD and
ATTACH statements are written out once per allowed value. These tests pin
that each literal is exactly the statement the old f-string composed."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

from agents import agent_manager
from agents import duckdb_tools as dt


def test_one_load_statement_per_extension_equal_to_the_old_composition():
    assert set(dt._LOAD_SQL) == set(dt._DUCKDB_EXTENSIONS)
    for ext in dt._DUCKDB_EXTENSIONS:
        assert dt._LOAD_SQL[ext] == f"LOAD {ext}"


def test_one_attach_statement_per_endpoint_equal_to_the_old_composition():
    assert set(dt._ATTACH_SQL) == set(dt._CATALOG_ENDPOINTS)
    for endpoint in dt._CATALOG_ENDPOINTS:
        assert dt._ATTACH_SQL[endpoint] == (
            f"ATTACH ? AS {dt.CATALOG} (TYPE iceberg, ENDPOINT_TYPE {endpoint}, READ_ONLY)")


def test_set_up_runs_the_fixed_statements_in_order(monkeypatch):
    monkeypatch.setattr(dt, "_secret_statement", lambda cfg: ("CREATE SECRET x ()", [], None))
    monkeypatch.setattr(dt, "_attach_target", lambda cfg: ("arn:target", "glue"))
    reader = dt._S3TablesReader.__new__(dt._S3TablesReader)
    reader._cfg = {}
    con = MagicMock()
    assert reader._set_up(con) is None
    sent = [c.args for c in con.execute.call_args_list]
    assert sent[:4] == [(f"LOAD {e}",) for e in dt._DUCKDB_EXTENSIONS]
    assert sent[-1] == (dt._ATTACH_SQL["glue"], ["arn:target"])


def test_a_failed_close_is_logged_at_debug_and_not_raised(caplog):
    con = MagicMock()
    con.close.side_effect = RuntimeError("already closed")
    with caplog.at_level(logging.DEBUG, logger=dt.logger.name):
        dt._S3TablesReader._close_quietly(con)
    assert any("RuntimeError" in r.getMessage() and r.levelno == logging.DEBUG
               for r in caplog.records)


def test_a_failed_interrupt_is_logged_at_debug_and_not_raised(caplog):
    cur = MagicMock()
    cur.interrupt.side_effect = RuntimeError("query already finished")
    stopper = dt._QueryStopper(cur, 3600)
    with caplog.at_level(logging.DEBUG, logger=dt.logger.name):
        stopper._stop()
    stopper.finish()
    assert any("RuntimeError" in r.getMessage() and r.levelno == logging.DEBUG
               for r in caplog.records)


def test_a_failed_stage_restore_is_logged_at_debug_then_rewritten(caplog, monkeypatch):
    sm = MagicMock()
    sm.update_secret_version_stage.side_effect = RuntimeError("throttled")
    sm.get_secret_value.return_value = {"VersionId": "v-new", "SecretString": "{}"}
    put = MagicMock()
    monkeypatch.setattr(agent_manager, "_cas_put_payload", put)
    with caplog.at_level(logging.DEBUG, logger=agent_manager.logger.name):
        out = agent_manager._restore_baseline(
            sm, "adt/agent-config/x", "x", "v-new", "v-base", {"k": "v"})
    assert out == "restored"
    put.assert_called_once_with(sm, "adt/agent-config/x", "v-new", {"k": "v"}, "x")
    assert any("RuntimeError" in r.getMessage() and r.levelno == logging.DEBUG
               for r in caplog.records)


def test_every_allowed_endpoint_attaches_with_its_own_statement():
    arn = "arn:aws:s3tables:us-east-1:111122223333:bucket/demo-bucket"
    for endpoint in dt._CATALOG_ENDPOINTS:
        _target, chosen = dt._attach_target(
            {"table_bucket_arn": arn, "catalog_endpoint": endpoint})
        assert chosen == endpoint
        assert chosen in dt._ATTACH_SQL
