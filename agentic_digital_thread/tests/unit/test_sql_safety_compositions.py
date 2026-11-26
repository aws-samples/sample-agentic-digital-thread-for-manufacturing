# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every combination of the spellings the SQL guard handles, around every
denied function, is refused before Mode A or Mode B connects.

A guard that handles each feature on its own can still miss two of them
together (an array slice ``[1:...]`` followed by a Unicode-escaped name once
ran ``pg_try_advisory_lock`` on PostgreSQL). So the compositions in
``_sql_guard_compositions`` (13 name spellings x 6 gaps x 10 positions, plus
13 spellings x 5 lexing wrappers: 845 per name) are checked against every
denied exact name and a real example of every denied prefix (35 names,
29,575 compositions), each three ways: ``ensure_select_only``, Mode A
(``_execute_direct_query``) and Mode B (``_mode_b_postgres_query``) with
connecting made to fail the test. The same compositions around allowed names
(``lower``; ``pg_size_pretty`` and ``ts_rank``, which start like denied
names) must pass wherever no Unicode escape or lexing refusal is involved.

``tests/integration/test_sql_guard_matches_postgres_pg.py`` runs the
compositions on PostgreSQL 16 around ``pg_try_advisory_lock`` and shows each
one takes the lock there.
"""
from __future__ import annotations

import pytest

from agents import system_agent
from agents.sql_safety import ensure_select_only
from tests.unit import _sql_guard_compositions as comp


class _Connected(Exception):
    """Raised by the patched connect functions: the guard let SQL through."""


@pytest.fixture
def no_connection(monkeypatch):
    def connect(*args, **kwargs):
        raise _Connected()

    monkeypatch.setattr(system_agent.pgdriver, "connect", connect)
    monkeypatch.setattr(system_agent, "_pg_readonly_conn", connect)


def _outcome(fn, sql) -> str:
    try:
        fn(sql)
    except ValueError:
        return "refused"
    except _Connected:
        return "connected"
    return "accepted"


_PATHS = (
    ("guard", ensure_select_only),
    ("mode_a", lambda sql: system_agent._execute_direct_query("mes", {"host": "db.example.com"}, sql)),
    ("mode_b", lambda sql: system_agent._mode_b_postgres_query(sql, "mes", {"host": "db.example.com"}, None)),
)


def test_the_composition_count_is_what_the_report_states():
    assert comp.count_per_name() == 845
    assert len(comp.DENIED_NAMES) == 35
    assert len(comp.DENIED_NAMES) * comp.count_per_name() == 29_575


@pytest.mark.parametrize("name", comp.DENIED_NAMES)
def test_every_composition_of_a_denied_name_is_refused_before_connecting(no_connection, name):
    misses = []
    for composition_id, sql, _by_form in comp.compositions(name):
        for path, fn in _PATHS:
            outcome = _outcome(fn, sql)
            if outcome != "refused":
                misses.append(f"{path} {outcome}: {composition_id}: {sql!r}")
    assert not misses, f"{len(misses)} misses for {name}:\n" + "\n".join(misses[:20])


@pytest.mark.parametrize("name", comp.ALLOWED_NAMES)
def test_the_same_compositions_of_an_allowed_name_pass(name):
    refused = []
    checked = 0
    for composition_id, sql, by_form in comp.compositions(name):
        if by_form:
            continue
        checked += 1
        try:
            ensure_select_only(sql)
        except ValueError as exc:
            refused.append(f"{composition_id}: {sql!r}: {exc}")
    assert checked == 8 * 5 * 10
    assert not refused, "\n".join(refused[:20])


@pytest.mark.parametrize("name", comp.ALLOWED_NAMES)
def test_a_unicode_escape_or_lexing_wrapper_is_refused_for_any_name(name):
    accepted = [composition_id for composition_id, sql, by_form in comp.compositions(name)
                if by_form and _outcome(ensure_select_only, sql) != "refused"]
    assert not accepted, accepted[:20]
