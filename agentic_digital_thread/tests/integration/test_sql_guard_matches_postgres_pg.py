# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Each spelling the SQL guard refuses for its lexing really calls the
function on PostgreSQL.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server. For every
spelling in ``tests/unit/test_sql_safety_postgres_lexing.py`` that PostgreSQL
runs, this executes it on a fresh database with ``pg_notify`` (seen by a
second session that LISTENs) or ``pg_try_advisory_lock`` (seen in
``pg_locks``) in the hidden position, and requires ``ensure_select_only`` to
refuse the same text. A plain spelling is the control. The backtick
spelling is the exception: PostgreSQL rejects it at parse analysis, because
no backtick operator exists, so nothing hidden in backticks runs.

It also runs every composition in ``tests/unit/_sql_guard_compositions.py``
(845) around ``pg_try_advisory_lock`` with its own key, checks that
PostgreSQL took that lock, and requires the guard to refuse the same text;
and it shows that an ordinary string ending in a backslash reads the same in
the guard and in PostgreSQL through the real Mode A and Mode B session
setup, even on a database whose default turns standard_conforming_strings
off.

Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_sql_guard_matches_postgres_pg.py -q
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents import system_agent  # noqa: E402
from agents.sql_safety import ensure_select_only  # noqa: E402
from tests.unit import _sql_guard_compositions as comp  # noqa: E402
from tests.integration._pg_sql import stmt  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_CHANNEL = "adt_guard_probe"
_NOTIFY = f"pg_notify('{_CHANNEL}', '{{case}}')"

#: case -> statement whose hidden call is ``pg_notify(channel, case)``.
NOTIFY_SPELLINGS = {
    "nested_comment": "SELECT pg_notify /* outer /* inner */ still outer */ ('" + _CHANNEL + "', '{case}')",
    "overlapping_nested_comment": "SELECT pg_notify /* a /*/ b */ */ ('" + _CHANNEL + "', '{case}')",
    "dollar_quote": "SELECT $$'$$ AS a, " + _NOTIFY + " AS b, $$'$$ AS c",
    "tagged_dollar_quote": "SELECT $q$'$q$ AS a, " + _NOTIFY + " AS b, $q$'$q$ AS c",
    "escape_string": "SELECT E'\\'' AS a, " + _NOTIFY + " AS b, E'\\'' AS c",
    "carriage_return_in_line_comment": "SELECT 1 -- note\r, " + _NOTIFY,
    "comment_inside_a_number": "SELECT 1--'\n, " + _NOTIFY + " --'",
}


def _verbatim(text: str):
    """*text* as a ``pgdriver.sql.SQL`` statement: the server receives it
    unchanged (the statement text is what these tests check)."""
    from agents.pgdriver import sql

    return sql.SQL(text)


@pytest.fixture
def pg():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    from agents import pgdriver
    from agents.pgdriver import make_dsn, parse_dsn

    name = "guard_" + uuid.uuid4().hex[:8]
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=name))

    def connect():
        conn = pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": name}))
        conn.autocommit = True
        return conn

    connect.name = name
    connect.params = {**parse_dsn(_DSN), "dbname": name}
    try:
        yield connect
    finally:
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE IF EXISTS {db} WITH (FORCE)", db=name))
        admin.close()


def _notifications(listener, wait: float = 2.0) -> list[str]:
    """The payloads the listener's session received within ``wait`` seconds.
    pg8000 reads a notification while it handles a statement, so this runs an
    empty query until one arrives; ``_raw.notifications`` is pg8000's own
    queue of ``(pid, channel, payload)``."""
    seen: list[str] = []
    deadline = time.monotonic() + wait
    while True:
        with listener.cursor() as cur:
            cur.execute("SELECT 1")
        queue = listener._raw.notifications
        while queue:
            seen.append(queue.popleft()[2])
        if seen or time.monotonic() >= deadline:
            return seen
        time.sleep(0.05)


@pytest.mark.parametrize("case", ["control", *sorted(NOTIFY_SPELLINGS)])
def test_postgres_runs_the_hidden_call_and_the_guard_refuses_it(pg, case):
    sql = ("SELECT " + _NOTIFY if case == "control" else NOTIFY_SPELLINGS[case]).format(case=case)
    listener, runner = pg(), pg()
    with listener.cursor() as cur:
        cur.execute(stmt("LISTEN {channel}", channel=_CHANNEL))
    with runner.cursor() as cur:
        cur.execute(_verbatim(sql))
    assert _notifications(listener) == [case], "PostgreSQL did not run the call"
    with pytest.raises(ValueError):
        ensure_select_only(sql)
    listener.close()
    runner.close()


def test_postgres_runs_a_call_inside_an_array_slice_and_the_guard_refuses_it(pg):
    key = 424242
    sql = f"SELECT (ARRAY[1, 2])[1:pg_try_advisory_lock({key})::int]"
    runner, observer = pg(), pg()
    with runner.cursor() as cur:
        cur.execute(_verbatim(sql))
    assert _holds_lock(observer, key, runner), "PostgreSQL did not take the lock"
    runner.close()
    observer.close()
    with pytest.raises(ValueError):
        ensure_select_only(sql)


def test_postgres_rejects_the_backtick_spelling_and_the_guard_refuses_it_too(pg):
    from agents import pgdriver

    sql = "SELECT 1 ` pg_notify('" + _CHANNEL + "', 'backtick') `"
    listener, runner = pg(), pg()
    with listener.cursor() as cur:
        cur.execute(stmt("LISTEN {channel}", channel=_CHANNEL))
    with runner.cursor() as cur, pytest.raises(pgdriver.Error):
        cur.execute(_verbatim(sql))
    assert _notifications(listener, wait=0.5) == []
    with pytest.raises(ValueError):
        ensure_select_only(sql)
    listener.close()
    runner.close()


def _holds_lock(observer, key: int, holder) -> bool:
    """Whether ``holder``'s session holds advisory lock ``key`` in this test's database.

    Scoped to the holder's backend and the current database, so a concurrent run on the same
    server that holds the same key does not count.
    """
    with observer.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s AND pid = %s"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
            (key, _backend_pid(holder)))
        return cur.fetchone()[0] == 1


def _backend_pid(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        return cur.fetchone()[0]


def test_lock_check_counts_only_the_lock_the_runner_holds(pg):
    """A concurrent run on the same server can hold the same key in its own database. The check
    must not report that lock as the runner's, or a composition that took no lock would pass."""
    from agents import pgdriver

    key = 600_000 + uuid.uuid4().int % 1_000_000
    other = pgdriver.connect(_DSN)
    other.autocommit = True
    runner, observer = pg(), pg()
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            assert cur.fetchone()[0], "the other session could not take its lock"
        with runner.cursor() as cur:
            cur.execute("SELECT 1")
        assert not _holds_lock(observer, key, runner), "counted another session's lock as the runner's"
        with runner.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            assert cur.fetchone()[0], "the runner could not take its lock"
        assert _holds_lock(observer, key, runner), "missed the runner's own lock"
    finally:
        other.close()
        runner.close()
        observer.close()


def test_postgres_runs_a_unicode_escaped_name_in_an_array_slice_and_the_guard_refuses_it(pg):
    sql = 'SELECT (ARRAY[1,2])[1:U&"\\0070g_try_advisory_lock"(424243)::int]'
    runner, observer = pg(), pg()
    with runner.cursor() as cur:
        cur.execute(sql)
    assert _holds_lock(observer, 424243, runner), "PostgreSQL did not take the lock"
    with pytest.raises(ValueError):
        ensure_select_only(sql)
    runner.close()
    observer.close()


def test_postgres_runs_every_composition_and_the_guard_refuses_each(pg):
    from agents import pgdriver

    runner, observer = pg(), pg()
    not_run, accepted, ran = [], [], 0
    for key, (composition_id, sql, _by_form) in enumerate(
            comp.compositions("pg_try_advisory_lock", args="({key})"), start=500_000):
        sql = sql.replace("{key}", str(key))
        try:
            with runner.cursor() as cur:
                cur.execute(sql)
        except pgdriver.Error as exc:
            not_run.append(f"{composition_id}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
            continue
        if not _holds_lock(observer, key, runner):
            not_run.append(f"{composition_id}: no lock")
            continue
        ran += 1
        try:
            ensure_select_only(sql)
            accepted.append(f"{composition_id}: {sql!r}")
        except ValueError:
            pass
    runner.close()
    observer.close()
    assert not not_run, "PostgreSQL did not run:\n" + "\n".join(not_run[:20])
    assert ran == comp.count_per_name() == 845
    assert not accepted, f"{len(accepted)} accepted:\n" + "\n".join(accepted[:20])


def _database_scs_off(pg):
    admin_conn = pg()
    with admin_conn.cursor() as cur:
        cur.execute(stmt("ALTER DATABASE {db} SET standard_conforming_strings = off", db=pg.name))
    admin_conn.close()


@pytest.mark.parametrize("mode", ["mode_a", "mode_b"])
def test_an_ordinary_string_ending_in_a_backslash_reads_the_same_in_both_modes(pg, mode):
    """The database default is off; the system-agent session turns it on, so
    PostgreSQL reads 'C:\\' the way the guard does (one backslash)."""
    _database_scs_off(pg)
    probe = pg()
    with probe.cursor() as cur:
        cur.execute("SHOW standard_conforming_strings")
        assert cur.fetchone()[0] == "off"
    probe.close()
    sql = "SELECT 'C:\\' AS p, 'x\\' || 'y' AS q"
    assert ensure_select_only(sql) == sql
    if mode == "mode_a":
        result, _ = system_agent._execute_direct_query("public", dict(pg.params), sql)
    else:
        result = system_agent._mode_b_postgres_query(sql, "public", dict(pg.params), None)
    assert result.get("error") is None, result
    rows = result["rows"]
    first = rows[0] if isinstance(rows[0], (list, tuple)) else [rows[0]["p"], rows[0]["q"]]
    assert list(first) == ["C:\\", "x\\y"], rows


def test_a_call_after_a_backslash_ended_string_is_seen_by_both(pg):
    """With standard_conforming_strings on, 'x\\' ends at its quote in both
    readings, so the call after it is code to PostgreSQL and to the guard."""
    sql = "SELECT 'x\\' AS a, pg_try_advisory_lock(424244) AS b, '\\' AS c"
    with pytest.raises(ValueError):
        ensure_select_only(sql)
    runner, observer = pg(), pg()
    with runner.cursor() as cur:
        cur.execute("SET standard_conforming_strings = on")
        cur.execute(sql)
    assert _holds_lock(observer, 424244, runner)
    runner.close()
    observer.close()
