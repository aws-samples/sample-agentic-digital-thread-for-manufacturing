# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lifecycle of the shared warm engines behind ``duckdb_tools.run_select``.

* The cache is a bounded, thread-safe LRU; an evicted engine is closed, but
  only after the last in-flight read on it finishes.
* Every cached engine is closed at shutdown.
* A credential refresh never closes a connection another thread is still
  querying (connection generations are retired, then closed on last release).
* Distinct namespace / external id / role configs never share an engine.
"""
from __future__ import annotations

import threading
import time

import duckdb
import pytest

from agents import duckdb_tools as dt

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
ROLE = "arn:aws:iam::444455556666:role/data-reader"


def _base_connection():
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.plant")
    con.execute("CREATE TABLE s3t.plant.t(x INTEGER)")
    con.execute("INSERT INTO s3t.plant.t VALUES (1), (2), (3)")
    return con


@pytest.fixture(autouse=True)
def _clean_cache():
    close_all = getattr(dt, "_close_shared_handles", lambda: None)
    close_all()
    yield
    close_all()


class _Tracked:
    """A connection proxy that records close() and refuses use once closed."""

    def __init__(self, gate=None):
        self._con = _base_connection()
        self.closed = False
        self.gate = gate

    def cursor(self):
        return _Cursor(self, self._con.cursor())

    def execute(self, *a, **k):
        return self._con.execute(*a, **k)

    def close(self):
        self.closed = True
        self._con.close()


class _Cursor:
    def __init__(self, owner, cur):
        self._owner, self._cur = owner, cur

    def execute(self, sql, params=()):
        gate = self._owner.gate
        if gate is not None:
            gate["entered"].set()
            gate["release"].wait(5)
        if self._owner.closed:
            raise duckdb.ConnectionException("Connection already closed")
        return self._cur.execute(sql, params)

    def fetchmany(self, n):
        return self._cur.fetchmany(n)

    def close(self):
        try:
            self._cur.close()
        except Exception:  # noqa: BLE001 - closing a cursor of a closed connection
            pass


# --- credential refresh vs in-flight query (finding 4) -----------------------

def _reader_with_generations():
    made: list[_Tracked] = []
    gate = {"entered": threading.Event(), "release": threading.Event()}

    def factory():
        con = _Tracked(gate=gate if not made else None)
        made.append(con)
        return con

    reader = dt._S3TablesReader({"table_bucket_arn": ARN, "max_rows": 100},
                                connection_factory=factory)
    return reader, made, gate


def _stale(reader):
    import datetime as _dt
    reader._expiry = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)


def test_a_refresh_does_not_close_a_connection_a_query_is_using():
    reader, made, gate = _reader_with_generations()
    result = {}

    def slow_query():
        try:
            result["rows"] = reader.fetch("SELECT x FROM s3t.plant.t ORDER BY x")
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    first = threading.Thread(target=slow_query)
    first.start()
    assert gate["entered"].wait(5)           # query one holds generation one
    _stale(reader)
    second = reader.fetch("SELECT COUNT(*) FROM s3t.plant.t")  # forces a refresh
    assert len(made) == 2 and int(second[0][0]) == 3
    assert made[0].closed is False           # still in use: must stay open
    gate["release"].set()
    first.join(5)
    assert "error" not in result, result.get("error")
    assert [int(r[0]) for r in result["rows"]] == [1, 2, 3]
    assert made[0].closed is True            # closed once its last reader left


def test_an_idle_old_generation_is_closed_at_refresh():
    reader, made, gate = _reader_with_generations()
    gate["release"].set()
    reader.fetch("SELECT 1")
    _stale(reader)
    reader.fetch("SELECT 1")
    assert len(made) == 2 and made[0].closed is True and made[1].closed is False


def test_many_threads_across_a_refresh_all_succeed():
    reader, made, gate = _reader_with_generations()
    gate["release"].set()
    reader.fetch("SELECT 1")
    errors, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            for _ in range(20):
                reader.fetch("SELECT COUNT(*) FROM s3t.plant.t")
                _stale(reader)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []


def test_closing_the_handle_waits_for_a_running_query():
    reader, made, gate = _reader_with_generations()
    handle = dt._EngineHandle({"table_bucket_arn": ARN}, connection_factory=lambda: None)
    handle._reader = reader
    done = threading.Thread(target=lambda: reader.fetch("SELECT 1"))
    done.start()
    assert gate["entered"].wait(5)
    handle.close()
    assert made[0].closed is False
    gate["release"].set()
    done.join(5)
    assert made[0].closed is True


# --- bounded cache (finding 3) -----------------------------------------------

def _select(cfg, factory):
    return dt.run_select(cfg, "SELECT COUNT(*) FROM s3t.plant.t",
                         connection_factory=factory)


def _cfg(n):
    return {"table_bucket_arn": ARN, "duckdb_threads": n}


def test_the_cache_is_bounded_and_closes_what_it_evicts(monkeypatch):
    monkeypatch.setattr(dt, "_SHARED_HANDLES_MAX", 2)
    closed = []
    real_close = dt._EngineHandle.close
    monkeypatch.setattr(dt._EngineHandle, "close",
                        lambda self: (closed.append(self), real_close(self))[0])
    factory = _base_connection
    for n in (1, 2, 3):
        _select(_cfg(n), factory)
    assert len(dt._SHARED_HANDLES) == 2
    assert len(closed) == 1


def test_the_least_recently_used_engine_is_the_one_evicted(monkeypatch):
    monkeypatch.setattr(dt, "_SHARED_HANDLES_MAX", 2)
    factory = _base_connection
    _select(_cfg(1), factory)
    _select(_cfg(2), factory)
    _select(_cfg(1), factory)          # touch 1, so 2 is the oldest
    _select(_cfg(3), factory)
    kept = {k[0] for k in dt._SHARED_HANDLES}
    assert dt._config_digest(dt._resolve_config(_cfg(2))) not in kept
    assert dt._config_digest(dt._resolve_config(_cfg(1))) in kept


def test_an_evicted_engine_in_use_is_closed_only_after_its_read(monkeypatch):
    monkeypatch.setattr(dt, "_SHARED_HANDLES_MAX", 1)
    gate = {"entered": threading.Event(), "release": threading.Event()}
    con = _Tracked(gate=gate)
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=dt.run_select(_cfg(1), "SELECT COUNT(*) FROM s3t.plant.t",
                        connection_factory=lambda: con)))
    t.start()
    assert gate["entered"].wait(5)
    _select(_cfg(2), _base_connection)      # evicts the engine mid-read
    assert con.closed is False
    gate["release"].set()
    t.join(5)
    assert int(out["r"]["rows"][0][0]) == 3
    assert con.closed is True


def test_shutdown_closes_every_cached_engine():
    handles = []
    real = dt._EngineHandle.close
    for n in (1, 2, 3):
        _select(_cfg(n), _base_connection)
    handles = [e.handle for e in dt._SHARED_HANDLES.values()]
    closed = []
    for h in handles:
        h.close = (lambda hh=h: closed.append(hh) or real(hh))
    dt._close_shared_handles()
    assert len(closed) == 3 and not dt._SHARED_HANDLES


def test_a_shutdown_hook_is_registered_once(monkeypatch):
    import atexit
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(dt, "_SHUTDOWN_REGISTERED", False)
    _select(_cfg(1), _base_connection)
    _select(_cfg(2), _base_connection)
    assert registered == [dt._close_shared_handles]


def test_concurrent_callers_of_one_config_build_one_engine(monkeypatch):
    built = []
    real = dt._EngineHandle

    class _Counting(real):
        def __init__(self, cfg, connection_factory=None):
            time.sleep(0.01)
            built.append(cfg)
            super().__init__(cfg, connection_factory=connection_factory)

    monkeypatch.setattr(dt, "_EngineHandle", _Counting)
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        barrier.wait()
        try:
            _select(_cfg(1), _factory_singleton)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and len(built) == 1


_SINGLETON = {}


def _factory_singleton():
    return _SINGLETON.setdefault("con", _base_connection())


# --- isolation (finding 5) ---------------------------------------------------

def test_engines_are_not_shared_across_scope_or_credentials():
    base = {"table_bucket_arn": ARN}
    variants = [
        base,
        {**base, "namespaces": ["plant"]},
        {**base, "namespaces": ["other"]},
        {**base, "role_arn": ROLE, "external_id": "ext-1"},
        {**base, "role_arn": ROLE, "external_id": "ext-2"},
        {**base, "catalog_endpoint": "glue"},
    ]
    for cfg in variants:
        dt.run_select(cfg, "SELECT 1", connection_factory=_base_connection)
    assert len(dt._SHARED_HANDLES) == len(variants)


def test_the_cache_key_never_holds_the_external_id():
    cfg = {"table_bucket_arn": ARN, "role_arn": ROLE, "external_id": "very-secret-id"}
    dt.run_select(cfg, "SELECT 1", connection_factory=_base_connection)
    assert "very-secret-id" not in repr(list(dt._SHARED_HANDLES.keys()))
