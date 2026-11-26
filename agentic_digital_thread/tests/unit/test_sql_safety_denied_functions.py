# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``ensure_select_only`` refuses functions with an effect outside the query,
and Mode A runs it before executing.

A read-only transaction stops table writes, not a ``SELECT`` that ends other
sessions, reads server files, takes advisory locks, changes settings or runs
SQL held in a text value. The database login is the real boundary (a system
agent's login reads its own schema only); this guard is defence in depth, on
every model-written statement: Mode A (``_execute_direct_query``), Mode B and
Athena. Legitimate read-only SQL keeps passing, including shapes the earlier
text check refused (a leading parenthesis, a ``;`` inside a string).
"""
from __future__ import annotations

import pytest

from agents import system_agent
from agents.sql_safety import ensure_select_only

DENIED = [
    "SELECT pg_terminate_backend(123)",
    "SELECT pg_cancel_backend(123)",
    "SELECT pg_reload_conf()",
    "SELECT pg_rotate_logfile()",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT * FROM pg_ls_dir('.')",
    "SELECT pg_ls_logdir()",
    "SELECT pg_stat_file('postgresql.conf')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(1, '/tmp/x')",
    "SELECT * FROM dblink('host=example.com', 'SELECT 1') AS t(a int)",
    "SELECT dblink_exec('host=example.com', 'DROP TABLE t')",
    "SELECT pg_advisory_lock(42)",
    "SELECT pg_advisory_xact_lock(42)",
    "SELECT pg_try_advisory_lock(42)",
    "SELECT set_config('statement_timeout', '0', false)",
    "SELECT pg_sleep(30)",
    "SELECT pg_sleep_for('30 seconds')",
    "SELECT query_to_xml('select pg_terminate_backend(1)', true, true, '')",
    "SELECT ts_stat('select vector from t')",
    "SELECT pg_notify('channel', 'x')",
]


@pytest.mark.parametrize("sql", DENIED)
def test_denied_functions_are_refused(sql):
    with pytest.raises(ValueError, match="not allowed"):
        ensure_select_only(sql)


@pytest.mark.parametrize("sql", [
    "SELECT pg_catalog.pg_terminate_backend(123)",
    'SELECT "pg_terminate_backend"(123)',
    "SELECT PG_TERMINATE_BACKEND(123)",
    "SELECT pg_terminate_backend /* x */ (123)",
    "SELECT pg_terminate_backend\n(123)",
    "WITH x AS (SELECT pg_sleep(5)) SELECT * FROM x",
    "SELECT a FROM t WHERE b = (SELECT set_config('role', 'postgres', true))",
    "SELECT id FROM t UNION ALL SELECT pg_cancel_backend(pid) FROM pg_stat_activity",
])
def test_denied_functions_are_refused_however_they_are_spelled(sql):
    with pytest.raises(ValueError, match="not allowed"):
        ensure_select_only(sql)


@pytest.mark.parametrize("sql", [
    'SELECT U&"\\0070g_sleep"(1)',
    "SELECT u&'\\0070g_sleep'",
])
def test_unicode_escapes_are_refused(sql):
    with pytest.raises(ValueError, match="Unicode"):
        ensure_select_only(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE note = 'call pg_sleep(1) later'",
    "SELECT pg_sleep_minutes, set_config_name FROM settings",
    "SELECT count(*), lower(name), coalesce(a, b) FROM t GROUP BY 2",
    "SELECT * FROM t WHERE note LIKE '%;%'",
    "(SELECT a FROM t1) UNION ALL (SELECT a FROM t2)",
    "SELECT current_setting('timezone')",
    "SELECT 1; -- done",
    "SELECT u & 1 FROM flags",
])
def test_read_only_sql_still_passes(sql):
    assert ensure_select_only(sql) == sql


@pytest.mark.parametrize("sql", [
    "SELECT 'unterminated",
    "SELECT (1",
])
def test_sql_that_cannot_be_read_with_certainty_is_refused(sql):
    with pytest.raises(ValueError):
        ensure_select_only(sql)


def test_mode_a_refuses_a_denied_function_before_connecting(monkeypatch):
    def _no_connect(**kwargs):
        raise AssertionError("Mode A connected for a refused statement")

    monkeypatch.setattr(system_agent.pgdriver, "connect", _no_connect)
    with pytest.raises(ValueError, match="not allowed"):
        system_agent._execute_direct_query("mes", {"host": "db.example.com"},
                                           "SELECT pg_terminate_backend(123)")


def test_mode_a_refuses_a_mutating_statement_before_connecting(monkeypatch):
    monkeypatch.setattr(system_agent.pgdriver, "connect",
                        lambda **kw: pytest.fail("Mode A connected for a refused statement"))
    with pytest.raises(ValueError, match="read-only"):
        system_agent._execute_direct_query("mes", {"host": "db.example.com"},
                                           "DELETE FROM work_orders")


def test_mode_b_refuses_a_denied_function_before_connecting(monkeypatch):
    monkeypatch.setattr(system_agent, "_pg_readonly_conn",
                        lambda params: pytest.fail("Mode B connected for a refused statement"))
    with pytest.raises(ValueError, match="not allowed"):
        system_agent._mode_b_postgres_query("SELECT pg_advisory_lock(1)", "mes",
                                            {"host": "db.example.com"}, None)
