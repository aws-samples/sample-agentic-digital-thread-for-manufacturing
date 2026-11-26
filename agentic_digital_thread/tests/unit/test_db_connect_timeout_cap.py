# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``db.connect_timeout_cap`` bounds connecting for the reads inside it.

``query_pg`` / ``pg`` open a fresh connection per read, and the statement
deadline they set only starts once it is open: a black-holed endpoint, a
stalled authentication or a server out of connection slots could hold a
read far past any deadline its caller budgeted. Inside the cap every
connection this module opens passes libpq's ``connect_timeout`` (whole
seconds, at least 1), a stricter configured one still wins, and outside it
nothing changes for any caller.
"""
from __future__ import annotations

import pytest

import agents.db as db
from agents import pgdriver

_PARAMS = {"host": "h", "port": "5432", "dbname": "d", "user": "u", "password": "p"}


@pytest.fixture
def params(monkeypatch):
    """The resolved connection params (records-first path, cache cleared)."""
    holder = {"params": dict(_PARAMS)}
    monkeypatch.setattr(db, "_SCHEMA_CONN_CACHE", {})
    monkeypatch.setattr(db, "_records_conn_params_for_schema",
                        lambda _schema: dict(holder["params"]))
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    return holder


def test_outside_a_cap_the_params_are_exactly_as_resolved(params):
    assert db._get_conn_params_for_schema("s") == _PARAMS
    assert db.current_connect_timeout_cap() is None


def test_inside_a_cap_the_connection_waits_at_most_that_long(params):
    with db.connect_timeout_cap(3):
        assert db.current_connect_timeout_cap() == 3
        assert db._get_conn_params_for_schema("s") == {**_PARAMS, "connect_timeout": 3}
    assert db._get_conn_params_for_schema("s") == _PARAMS
    assert db.current_connect_timeout_cap() is None


@pytest.mark.parametrize("seconds, cap", [(0, 1), (-5, 1), (2.9, 2), (1, 1)])
def test_the_cap_is_whole_seconds_and_at_least_one(params, seconds, cap):
    with db.connect_timeout_cap(seconds):
        assert db._get_conn_params_for_schema("s")["connect_timeout"] == cap


@pytest.mark.parametrize("configured, cap, used", [
    (1, 5, 1), ("1", 5, 1), (30, 2, 2), ("30", 2, 2), (0, 2, 2), (-1, 2, 2), ("x", 2, 2),
], ids=["stricter int", "stricter str", "looser int", "looser str", "zero is no limit",
        "negative is no limit", "not a number"])
def test_a_stricter_configured_connect_timeout_still_wins(params, configured, cap, used):
    params["params"] = {**_PARAMS, "connect_timeout": configured}
    with db.connect_timeout_cap(cap):
        assert db._get_conn_params_for_schema("s")["connect_timeout"] == used


def test_a_stricter_pgconnect_timeout_still_wins(params, monkeypatch):
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "1")
    with db.connect_timeout_cap(4):
        assert db._get_conn_params_for_schema("s")["connect_timeout"] == 1
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "60")
    with db.connect_timeout_cap(4):
        assert db._get_conn_params_for_schema("s")["connect_timeout"] == 4


def test_a_nested_cap_keeps_the_stricter_one(params):
    with db.connect_timeout_cap(2):
        with db.connect_timeout_cap(5):
            assert db.current_connect_timeout_cap() == 2
        with db.connect_timeout_cap(1):
            assert db.current_connect_timeout_cap() == 1
        assert db.current_connect_timeout_cap() == 2
    assert db.current_connect_timeout_cap() is None


def test_the_cap_is_lifted_when_the_block_raises(params):
    with pytest.raises(RuntimeError):
        with db.connect_timeout_cap(2):
            raise RuntimeError("read failed")
    assert db.current_connect_timeout_cap() is None
    assert "connect_timeout" not in db._get_conn_params_for_schema("s")


def test_a_stricter_configured_value_wins_over_pgconnect_timeout_and_the_cap(monkeypatch):
    # Both a configured connect_timeout and PGCONNECT_TIMEOUT: libpq reads the
    # configured one, so the cap compares against it, not the environment.
    monkeypatch.setattr(db, "_SCHEMA_CONN_CACHE", {})
    monkeypatch.setattr(db, "_records_conn_params_for_schema",
                        lambda _schema: {"host": "h", "dbname": "d", "connect_timeout": "1"})
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "30")
    with db.connect_timeout_cap(4):
        assert db._get_conn_params_for_schema("s")["connect_timeout"] == 1


def test_a_thread_started_inside_the_block_does_not_inherit_the_cap(params):
    # A ContextVar is per thread: the cap is for the synchronous caller only.
    import threading
    seen: list = []
    with db.connect_timeout_cap(2):
        t = threading.Thread(target=lambda: seen.append(db.current_connect_timeout_cap()))
        t.start()
        t.join()
    assert seen == [None]


@pytest.mark.parametrize("helper", ["query_pg", "pg"])
def test_query_pg_and_pg_connect_with_the_cap_and_only_inside_it(params, monkeypatch, helper):
    seen: list = []

    def connect(**kwargs):
        seen.append(kwargs.get("connect_timeout"))
        raise pgdriver.OperationalError("could not connect")
    monkeypatch.setattr(db.pgdriver, "connect", connect)
    read = getattr(db, helper)

    def call():
        try:
            read("s", "SELECT 1")
        except pgdriver.OperationalError:
            pass
    call()
    with db.connect_timeout_cap(2):
        call()
    call()
    assert seen == [None, 2, None]
