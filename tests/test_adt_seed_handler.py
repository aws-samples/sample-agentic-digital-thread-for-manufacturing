# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A failed demo seed file fails the request (lambda-adt-seed/handler.py).

The handler is the ``onEvent`` function of the ``aws_cdk.custom_resources``
Provider behind ``AdtSeedResource`` (stacks/adt_seed_data_stack.py). The
Provider reports the custom resource FAILED only when the handler RAISES; a
returned value is sent to CloudFormation as SUCCESS. So a seed file that
fails must raise, or a new Workshop Studio account reports CREATE_COMPLETE
while the CMMS/PLM/MES/historian story data is missing or partial. The app's
Reset All (``POST /api/onboard/reseed``, agents/onboard.py) invokes the same
function directly and reports its FunctionError.

Every file still runs after one fails, and the error names every failed file
and the first error.

The rollback of a failed Update reaching the new code (it asks for the
PREVIOUS ``Version``; the function still ships the new SQL files) is skipped:
re-running the new files would fail the same way and leave the stack in
UPDATE_ROLLBACK_FAILED. Only CloudFormation requests (a ``StackId``) that
carry a ``Version`` are compared, so the Reset All payload always runs.

Offline tests run the handler on stand-in SQL files and a fake connection;
the ``pg_*`` tests run it on the bundled files against a throwaway database
(ERP_SIM_TEST_PG_DSN) and are skipped without it.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import types
import uuid
from pathlib import Path

import pytest

from _pg_sql import pgdriver, stmt

_REPO = Path(__file__).resolve().parents[1]
_SEED_DIR = _REPO / "lambda-adt-seed"
_SUCCESS = {"PhysicalResourceId": "vf-seed", "Data": {"Status": "Complete"}}
_ENV = ("ADT_SEED_VERSION", "LAMBDA_TASK_ROOT", "DB_HOST", "DB_NAME", "DB_SECRET_ARN")


@pytest.fixture(scope="module")
def handler():
    stand_ins = {}
    for name in ("boto3",):                       # pgdriver is imported when connecting
        if name not in sys.modules:
            try:
                __import__(name)
            except ImportError:
                sys.modules[name] = types.ModuleType(name)
                stand_ins[name] = True
    try:
        spec = importlib.util.spec_from_file_location("adt_seed_handler_failures", _SEED_DIR / "handler.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name in stand_ins:
            del sys.modules[name]


def _call(handler, event):
    """handler(event); its printed log is returned with the result (or the
    raised exception) as (outcome, log)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            return handler.handler(event, None), out.getvalue()
        except Exception as exc:  # noqa: BLE001 - returned for the test to assert on
            return exc, out.getvalue()


# --------------------------------------------------------------------------
# Offline: stand-in SQL files, a fake connection
# --------------------------------------------------------------------------
_FILES = ("001_a.sql", "002_b.sql", "003_c.sql")


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        name = sql.split("\n", 1)[0].removeprefix("-- ")
        self.conn.executed.append(name)
        if name in self.conn.fail:
            raise self.conn.fail[name]

    def close(self):
        pass


class _Conn:
    def __init__(self):
        self.autocommit = False
        self.executed = []
        self.fail = {}
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def seed(handler, monkeypatch, tmp_path):
    """The handler on three stand-in SQL files (each one's first line names
    it) and a fake connection; every connection opened is recorded. A test
    makes a file fail with ``state["conn"].fail[<name>] = <exception>``. The
    function's environment starts without ADT_SEED_VERSION."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    for name in _FILES:
        (tmp_path / name).write_text(f"-- {name}\nSELECT 1;\n", encoding="utf-8")
    (tmp_path / "handler.py").write_text("# not SQL\n", encoding="utf-8")
    monkeypatch.setenv("LAMBDA_TASK_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_HOST", "db.example")
    monkeypatch.setenv("DB_NAME", "adt_demo")
    monkeypatch.setenv("DB_SECRET_ARN", "arn:secret")
    state = {"conn": _Conn(), "opened": [], "dir": tmp_path}

    def connect(**_kwargs):
        state["opened"].append(state["conn"])
        return state["conn"]

    monkeypatch.setattr(handler, "get_db_credentials", lambda: ("postgres", "pw"))
    monkeypatch.setitem(sys.modules, "pgdriver", types.SimpleNamespace(connect=connect))
    return state


_V0, _V1 = "0000000000000000", "1111111111111111"
# Every CloudFormation request carries its stack's id (the Provider forwards
# the whole request); a direct invoke does not.
_STACK = "arn:aws:cloudformation:us-east-1:111122223333:stack/ADTSeedDataStack/x"
# Reset All: agents/onboard.py onboard_reseed invokes the function with this.
_RESEED = {"RequestType": "Update", "ResourceProperties": {}}


def _props(version):
    return {"ServiceToken": "arn:provider", "Version": version}


def _create(version=_V1):
    return {"RequestType": "Create", "StackId": _STACK, "ResourceProperties": _props(version)}


def _update(old, new):
    return {"RequestType": "Update", "StackId": _STACK, "PhysicalResourceId": "vf-seed",
            "OldResourceProperties": _props(old), "ResourceProperties": _props(new)}


_RUNS = [
    pytest.param(_create(), id="create"),
    pytest.param(_update(_V0, _V1), id="forward-update"),
    pytest.param(_RESEED, id="reset-all-reseed"),
]
_FAILURES = [
    pytest.param({"002_b.sql": RuntimeError("relation \"x\" does not exist")}, id="one-file"),
    pytest.param({"001_a.sql": RuntimeError("permission denied for database adt_demo"),
                  "003_c.sql": ValueError("schema \"y\" does not exist")}, id="two-files"),
]


@pytest.mark.parametrize("event", _RUNS)
def test_every_file_runs_and_returns_the_same_success(handler, seed, monkeypatch, event):
    monkeypatch.setenv("ADT_SEED_VERSION", _V1)
    result, log = _call(handler, event)
    assert result == _SUCCESS
    assert seed["conn"].executed == list(_FILES)
    assert seed["conn"].autocommit is True and seed["conn"].closed
    assert log.count("executed successfully") == 3 and "ERROR in" not in log


@pytest.mark.parametrize("event", _RUNS)
@pytest.mark.parametrize("fail", _FAILURES)
def test_a_failed_file_fails_the_request_after_every_file_ran(handler, seed, monkeypatch, event, fail):
    """Raised to the Provider (FAILED), naming every failed file and the
    first error; the files after a failed one still run; the connection is
    closed."""
    monkeypatch.setenv("ADT_SEED_VERSION", _V1)
    seed["conn"].fail = dict(fail)
    raised, log = _call(handler, event)
    assert isinstance(raised, Exception), f"returned {raised!r}"
    names = sorted(fail)
    first = fail[names[0]]
    assert str(raised) == (f"Demo seed failed: {len(names)} of 3 SQL files failed: {', '.join(names)}. "
                           f"First error, in {names[0]}: {type(first).__name__}: {first}")
    assert seed["conn"].executed == list(_FILES)
    assert seed["conn"].closed
    for name in names:
        assert f"ERROR in {name}" in log


def test_without_sql_files_the_request_fails(handler, seed):
    for name in _FILES:
        (seed["dir"] / name).unlink()
    raised, _log = _call(handler, _create())
    assert isinstance(raised, Exception), f"returned {raised!r}"
    assert str(raised) == f"Demo seed failed: no SQL files in {seed['dir']}"


def test_delete_never_touches_the_database(handler, seed):
    event = {"RequestType": "Delete", "StackId": _STACK, "PhysicalResourceId": "vf-seed",
             "ResourceProperties": _props(_V1)}
    result, _log = _call(handler, event)
    assert result == {"PhysicalResourceId": "vf-seed"}
    assert seed["opened"] == []


# --------------------------------------------------------------------------
# The rollback guard. A failed Update rolls back with CloudFormation sending
# the custom resource another Update with the PREVIOUS Version. The stack
# gives the function ADT_SEED_VERSION = adt_seed_version(), the same literal
# as the custom resource's Version (tests/test_seed_cr_versions.py). A
# CloudFormation Update asking for another Version is logged, skipped and
# succeeds. Without ADT_SEED_VERSION (a function deployed before the stack set
# it), or without a Version or a StackId (the Reset All invoke), it runs.
# --------------------------------------------------------------------------
def test_a_rollback_update_on_the_new_code_skips_and_succeeds(handler, seed, monkeypatch):
    monkeypatch.setenv("ADT_SEED_VERSION", _V1)
    seed["conn"].fail = {"002_b.sql": RuntimeError("would fail again")}
    result, log = _call(handler, _update(_V1, _V0))
    assert result == {"PhysicalResourceId": "vf-seed", "Data": {"Status": "Skipped"}}
    assert seed["opened"] == [] and seed["conn"].executed == []
    assert (f"Seed skipped: the update asks for Version {_V0!r} (this code: {_V1!r}), "
            "an update rollback reaching the new code; nothing changed") in log


def test_a_forward_update_for_this_code_runs_and_fails_loud(handler, seed, monkeypatch):
    monkeypatch.setenv("ADT_SEED_VERSION", _V1)
    assert _call(handler, _update(_V0, _V1))[0] == _SUCCESS
    seed["conn"].executed.clear()
    seed["conn"].fail = {"003_c.sql": RuntimeError("boom")}
    raised, _log = _call(handler, _update(_V0, _V1))
    assert isinstance(raised, Exception) and "003_c.sql" in str(raised)
    assert seed["conn"].executed == list(_FILES)


@pytest.mark.parametrize("env", [None, ""], ids=["unset", "empty"])
def test_without_a_pinned_version_every_update_runs_as_before(handler, seed, monkeypatch, env):
    if env is not None:
        monkeypatch.setenv("ADT_SEED_VERSION", env)
    assert _call(handler, _update(_V1, _V0))[0] == _SUCCESS
    assert seed["conn"].executed == list(_FILES)
    seed["conn"].fail = {"001_a.sql": RuntimeError("boom")}
    raised, _log = _call(handler, _update(_V1, _V0))
    assert isinstance(raised, Exception) and "001_a.sql" in str(raised)


@pytest.mark.parametrize("event", [
    pytest.param(_RESEED, id="reset-all-reseed"),
    pytest.param({"RequestType": "Update"}, id="no-properties"),
    pytest.param({"RequestType": "Update", "ResourceProperties": _props(_V0)}, id="version-without-stack-id"),
    pytest.param({"RequestType": "Update", "StackId": _STACK, "ResourceProperties": {}}, id="stack-id-without-version"),
])
def test_an_update_that_is_not_a_versioned_cloudformation_request_always_runs(handler, seed, monkeypatch,
                                                                              event):
    """The Reset All invoke carries no StackId and no Version: it re-runs every
    file on a function whose stack pins the version, and a failure raises (the
    route reports the FunctionError)."""
    monkeypatch.setenv("ADT_SEED_VERSION", _V1)
    assert _call(handler, dict(event))[0] == _SUCCESS
    assert seed["conn"].executed == list(_FILES)
    seed["conn"].executed.clear()
    seed["conn"].fail = {"002_b.sql": RuntimeError("boom")}
    raised, _log = _call(handler, dict(event))
    assert isinstance(raised, Exception) and "002_b.sql" in str(raised)
    assert seed["conn"].executed == list(_FILES)


def test_create_is_never_skipped(handler, seed, monkeypatch):
    monkeypatch.setenv("ADT_SEED_VERSION", _V1)
    assert _call(handler, _create(_V0))[0] == _SUCCESS
    assert seed["conn"].executed == list(_FILES)


# --------------------------------------------------------------------------
# The FAILED response fits CloudFormation's limit ("The total size of the
# response body can't exceed 4096 bytes"). The Provider sends it as
# JSON.stringify of {Status, Reason, StackId, RequestId, PhysicalResourceId,
# LogicalResourceId}, its Reason being "Error: <message>\n\nLogs:
# /aws/lambda/<function>\n\n" and at most 10 Node.js frames of the
# framework's own code. The db-init fail-loud change
# (erp_simulation/tests/test_db_init_reset_failures.py) ran that framework
# from aws-cdk-lib 2.261.0 on node and measured it; this builds the same
# upper bound: the longest identifiers CloudFormation allows and 10 frames
# each about twice the longest it measured.
# --------------------------------------------------------------------------
_CFN_RESPONSE_LIMIT = 4096
_FUNCTION = "f" * 64
_LONG_STACK_ID = ("arn:aws-us-gov:cloudformation:" + "r" * 20 + ":111122223333:stack/"
                  + "S" * 128 + "/" + str(uuid.UUID(int=0)))
_FRAME = "    at " + "x" * 193
_HOSTILE_TEXTS = [
    pytest.param("😀" * 5000, id="four-byte"),
    pytest.param("工" * 5000, id="three-byte"),
    pytest.param("\x01" * 5000, id="control"),
    pytest.param('"\\' * 2500, id="quotes-and-backslashes"),
    pytest.param("\ud800" * 5000, id="lone-surrogates"),
    pytest.param("é😀\n\"工\x7f\t" * 1000, id="mixed"),
]


def _js_json_bytes(obj) -> bytes:
    """``JSON.stringify(obj)`` as UTF-8 (a lone surrogate escaped, as
    JSON.stringify does)."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    text = re.sub("[\ud800-\udfff]", lambda m: "\\u%04x" % ord(m.group()), text)
    return text.encode("utf-8")


def _provider_failed_body(error_message: str) -> bytes:
    message = "\n".join([error_message, "", f"Logs: /aws/lambda/{_FUNCTION}", ""])
    reason = "\n".join([f"Error: {message}"] + [_FRAME] * 10)
    return _js_json_bytes({"Status": "FAILED", "Reason": reason, "StackId": _LONG_STACK_ID,
                           "RequestId": str(uuid.UUID(int=1)),
                           "PhysicalResourceId": "AWSCDK::CustomResourceProviderFramework::CREATE_FAILED",
                           "LogicalResourceId": "L" * 255})


def test_the_message_limit_leaves_room_for_the_rest_of_the_response(handler):
    """The rest of the bounded FAILED body (handler.py's comment cites it)."""
    rest = len(_provider_failed_body(""))
    assert rest == 2801
    assert rest + handler._MESSAGE_BYTE_LIMIT < _CFN_RESPONSE_LIMIT


@pytest.mark.parametrize("text", _HOSTILE_TEXTS)
def test_a_long_failure_fits_the_providers_response(handler, seed, text):
    """A long error (counted in bytes, not characters) is cut so the
    Provider's FAILED response stays under 4096 bytes; the message still names
    the failed files and starts with the error."""
    seed["conn"].fail = {name: RuntimeError(text) for name in _FILES}
    raised, _log = _call(handler, _create())
    assert isinstance(raised, Exception), f"returned {raised!r}"
    message = str(raised)
    assert len(_provider_failed_body(message)) < _CFN_RESPONSE_LIMIT
    assert message.startswith("Demo seed failed: 3 of 3 SQL files failed: 001_a.sql, 002_b.sql, 003_c.sql. "
                              "First error, in 001_a.sql: RuntimeError: ")
    assert text[:10] in message and message.endswith("...")


def test_a_short_failure_message_is_not_cut(handler, seed):
    seed["conn"].fail = {"002_b.sql": RuntimeError("boom")}
    raised, _log = _call(handler, _create())
    assert str(raised) == ("Demo seed failed: 1 of 3 SQL files failed: 002_b.sql. "
                           "First error, in 002_b.sql: RuntimeError: boom")


# --------------------------------------------------------------------------
# Real PostgreSQL: ERP_SIM_TEST_PG_DSN (throwaway database); skipped otherwise.
# --------------------------------------------------------------------------
_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture
def pg_seed(handler, monkeypatch):
    """A throwaway database the handler connects to; dropped afterwards.
    Yields a function opening a new autocommit connection to it."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    driver = pgdriver()
    name = "seedfail_" + uuid.uuid4().hex[:8]
    dsn = driver.make_dsn(**{**driver.parse_dsn(_DSN), "dbname": name})
    admin = driver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=name))

    def connect(**_kwargs):
        conn = driver.connect(dsn)
        conn.autocommit = True
        return conn

    for env in _ENV:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("LAMBDA_TASK_ROOT", str(_SEED_DIR))
    monkeypatch.setenv("DB_HOST", "unused")
    monkeypatch.setenv("DB_NAME", name)
    monkeypatch.setenv("DB_SECRET_ARN", "unused")
    monkeypatch.setattr(handler, "get_db_credentials", lambda: ("unused", "unused"))
    monkeypatch.setitem(sys.modules, "pgdriver", types.SimpleNamespace(connect=connect))
    try:
        yield connect
    finally:
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(stmt("DROP DATABASE {db}", db=name))
        admin.close()


def _scalar(connect, query):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_pg_create_then_update_every_bundled_file_succeeds(handler, pg_seed, monkeypatch):
    """The bundled files on an empty database, then again on the seeded one:
    every file succeeds both times (so a failed file is never the normal
    state of a deploy)."""
    monkeypatch.setenv("ADT_SEED_VERSION", _V1)
    total = len(list(_SEED_DIR.glob("*.sql")))
    for event in (_create(_V1), _update(_V0, _V1)):
        result, log = _call(handler, event)
        assert result == _SUCCESS, log
        assert log.count("executed successfully") == total and "ERROR in" not in log
    assert _scalar(pg_seed, "SELECT count(*) FROM mes_line3.production_runs") == 159


def test_pg_a_failed_file_fails_the_create_leaves_nothing_and_the_rest_land(handler, pg_seed, monkeypatch,
                                                                             tmp_path):
    """A file that fails part-way leaves none of its statements (one query
    string runs as one implicit transaction), raises naming it, and the files
    after it still land."""
    for path in _SEED_DIR.glob("*.sql"):
        shutil.copy(path, tmp_path / path.name)
    (tmp_path / "002_broken.sql").write_text(
        "CREATE TABLE IF NOT EXISTS public.seed_probe (id int);\n"
        "INSERT INTO public.seed_probe VALUES (1);\n"
        "SELECT 1 / 0;\n", encoding="utf-8")
    monkeypatch.setenv("LAMBDA_TASK_ROOT", str(tmp_path))
    raised, log = _call(handler, _create())
    assert isinstance(raised, Exception), f"returned {raised!r}"
    total = len(list(tmp_path.glob("*.sql")))
    assert str(raised).startswith(f"Demo seed failed: 1 of {total} SQL files failed: 002_broken.sql. "
                                  "First error, in 002_broken.sql: DivisionByZero: division by zero")
    assert log.count("executed successfully") == total - 1
    assert _scalar(pg_seed, "SELECT to_regclass('public.seed_probe') IS NULL") is True
    assert _scalar(pg_seed, "SELECT count(*) FROM mes_line3.production_runs") == 159
