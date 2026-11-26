# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A failed Create or full-reset Update of the db-init custom resource fails
the stack (lambda-db-init/handler.py).

The handler is the ``onEvent`` function of an ``aws_cdk.custom_resources``
Provider (stacks/erp_database_stack.py). The Provider reports the custom
resource FAILED only when the handler RAISES; a returned
``{'Status': 'FAILED'}`` is sent to CloudFormation as SUCCESS (its ``Status``
key is never read). So a Create whose reset broke must raise, or a fresh
Workshop Studio account reports CREATE_COMPLETE over an empty or half-built
ERP database.

The rollback of a failed full-reset Update reaching the new code (its
properties are the PREVIOUS ones, the function still runs the new code) is
skipped, mirroring the seed-only guard:
re-running the new code's reset would fail the same way and leave the stack
in UPDATE_ROLLBACK_FAILED. The guard reads only CloudFormation requests: the
Streamlit dashboard's Reset All invokes the function directly with
``{'RequestType': 'Update'}`` and always gets the reset.

Offline tests use a fake connection that answers verify_schema's two queries;
the ``pg_*`` tests run the real reset on a throwaway database
(ERP_SIM_TEST_PG_DSN) and are skipped without it.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - runs node on a fixed driver file only
import sys
import tarfile
import traceback
import types
import uuid
from pathlib import Path

import pytest

from erp_simulation.tests._pg_sql import stmt

_HANDLER_PATH = Path(__file__).resolve().parents[2] / "lambda-db-init" / "handler.py"
_SUCCESS = {"Status": "SUCCESS", "Data": {"Message": "Database initialized and seeded successfully"}}
_RESET_STEPS = ["drop_all_tables", "create_schema", "seed_data"]
_LEGACY_SITES = ("MILWAUKEE_BOTTLING", "DETROIT_AUTOPARTS", "CEDARRAPIDS_FOOD")
_ENV = ("DB_INIT_TIMESTAMP", "DB_INIT_SEED_VERSION", "DB_NAME", "DB_CLUSTER_ENDPOINT", "DB_SECRET_ARN")


@pytest.fixture(scope="module")
def handler():
    stand_ins = {}
    for name in ("boto3",):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
            stand_ins[name] = True
    try:
        spec = importlib.util.spec_from_file_location("db_init_handler_reset_failures", _HANDLER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name in stand_ins:
            del sys.modules[name]


# --------------------------------------------------------------------------
# Offline: a fake connection whose cursor answers verify_schema
# --------------------------------------------------------------------------
class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.last = ("", None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append(" ".join(sql.split()))
        self.last = (sql, params)

    def fetchall(self):
        sql, params = self.last
        if "FROM pg_tables" in sql:
            return [(t,) for t in params[0] if t not in self.conn.missing_tables]
        if "FROM sites s" in sql:
            return [(site, self.conn.lots.get(site, 5)) for site in _LEGACY_SITES]
        raise AssertionError(f"unexpected query: {sql}")


class _Conn:
    def __init__(self, missing_tables=(), lots=None):
        self.missing_tables = set(missing_tables)
        self.lots = lots or {}
        self.statements = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


@pytest.fixture
def reset(handler, monkeypatch):
    """handler() on a fake connection: the three reset steps record their
    name (a test can make one raise), verify_schema runs for real against
    the fake cursor, every connection opened is recorded. The function's
    environment starts without the pinned variables."""
    state = {"conn": _Conn(), "steps": [], "opened": [], "fail": None}
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)

    def connect(_credentials):
        state["opened"].append(state["conn"])
        return state["conn"]

    monkeypatch.setattr(handler, "get_db_credentials", lambda: {"host": "h"})
    monkeypatch.setattr(handler, "get_connection", connect)
    for name in _RESET_STEPS:
        def step(conn, _name=name):
            state["steps"].append(_name)
            if state["fail"] == _name:
                raise RuntimeError(state.get("text") or f"boom in {_name}")
        monkeypatch.setattr(handler, name, step)
    return state


_P0 = {"ServiceToken": "arn:provider", "ClusterEndpoint": "db.example", "SecretArn": "arn:secret",
       "DatabaseName": "erp_simulation", "Timestamp": "T0", "SeedVersion": "a"}
_P1 = {**_P0, "Timestamp": "T1", "SeedVersion": "b"}
# Every CloudFormation request carries its stack's id (the Provider forwards
# the whole request); a direct invoke does not.
_STACK = "arn:aws:cloudformation:us-east-1:111122223333:stack/ADTERPDatabaseStack/x"
# The Streamlit dashboard's Reset All (erp_simulation/streamlit_dashboard.py)
# invokes the function directly with this payload and expects a full reset.
_DASHBOARD_RESET_ALL = {"RequestType": "Update"}


def _create(props=_P1) -> dict:
    return {"RequestType": "Create", "StackId": _STACK, "ResourceProperties": dict(props)}


def _update(old: dict, new: dict) -> dict:
    return {"RequestType": "Update", "StackId": _STACK, "PhysicalResourceId": "p",
            "OldResourceProperties": dict(old), "ResourceProperties": dict(new)}


def _pin_running_code(monkeypatch, props=_P1):
    """The environment the stack gives the function running ``props``' code."""
    monkeypatch.setenv("DB_INIT_TIMESTAMP", props["Timestamp"])
    monkeypatch.setenv("DB_NAME", props["DatabaseName"])
    monkeypatch.setenv("DB_INIT_SEED_VERSION", props["SeedVersion"])
    monkeypatch.setenv("DB_CLUSTER_ENDPOINT", props["ClusterEndpoint"])
    monkeypatch.setenv("DB_SECRET_ARN", props["SecretArn"])


_FULL_PATH_EVENTS = [
    pytest.param(_create(), id="create"),
    pytest.param(_update(_P0, _P1), id="timestamp-bump-update"),
    pytest.param(_update({**_P1, "DatabaseName": "erp_old"}, _P1), id="rename-update"),
    pytest.param(_DASHBOARD_RESET_ALL, id="dashboard-reset-all"),
]


@pytest.mark.parametrize("event", _FULL_PATH_EVENTS)
@pytest.mark.parametrize("failing", _RESET_STEPS)
def test_a_reset_step_failure_raises_naming_the_step(handler, reset, event, failing):
    """A step that raises fails the custom resource: raised to the Provider
    (not returned as FAILED), naming the step and the error, the steps after
    it never run, the connection is closed, no verification is attempted."""
    reset["fail"] = failing
    with pytest.raises(Exception) as raised:
        handler.handler(event, None)
    message = str(raised.value)
    assert failing in message and f"boom in {failing}" in message
    assert reset["steps"] == _RESET_STEPS[:_RESET_STEPS.index(failing) + 1]
    assert reset["conn"].closed
    assert not [s for s in reset["conn"].statements if "pg_tables" in s]


@pytest.mark.parametrize("event", _FULL_PATH_EVENTS)
def test_a_connection_failure_raises(handler, reset, monkeypatch, event):
    """Secrets Manager or the database unreachable: raised, nothing run."""
    def unreachable():
        raise ConnectionError("secret not readable")
    monkeypatch.setattr(handler, "get_db_credentials", unreachable)
    with pytest.raises(Exception, match="secret not readable"):
        handler.handler(event, None)
    assert reset["opened"] == [] and reset["steps"] == []


@pytest.mark.parametrize("event", _FULL_PATH_EVENTS)
def test_missing_tables_after_the_reset_raise_naming_them(handler, reset, event):
    """The reset ran but a table is missing: raised, the message names the
    verification and every missing table."""
    reset["conn"] = _Conn(missing_tables=("item_suppliers", "material_lots"))
    with pytest.raises(Exception) as raised:
        handler.handler(event, None)
    message = str(raised.value)
    assert "verif" in message
    assert "item_suppliers" in message and "material_lots" in message
    assert reset["steps"] == _RESET_STEPS and reset["conn"].closed


@pytest.mark.parametrize("event", _FULL_PATH_EVENTS)
def test_a_reset_that_lost_the_mes_equipment_registry_raises_naming_it(handler, reset, event):
    """seed_data creates mes_equipment (seed_mes_equipment), and the site
    views and the equipment pass read it: the verification after the reset
    names it when it is missing."""
    reset["conn"] = _Conn(missing_tables=("mes_equipment",))
    with pytest.raises(Exception) as raised:
        handler.handler(event, None)
    message = str(raised.value)
    assert "verif" in message and "mes_equipment" in message
    assert reset["conn"].closed


@pytest.mark.parametrize("event", _FULL_PATH_EVENTS)
def test_a_site_without_approved_lots_raises_naming_the_site(handler, reset, event):
    reset["conn"] = _Conn(lots={"DETROIT_AUTOPARTS": 0})
    with pytest.raises(Exception) as raised:
        handler.handler(event, None)
    message = str(raised.value)
    assert "DETROIT_AUTOPARTS" in message
    assert "MILWAUKEE_BOTTLING" not in message and "CEDARRAPIDS_FOOD" not in message
    assert reset["conn"].closed


@pytest.mark.parametrize("event", _FULL_PATH_EVENTS)
def test_a_verified_reset_returns_the_same_success(handler, reset, event):
    """Pinned (green before and after the change): success is byte-identical."""
    assert handler.handler(event, None) == _SUCCESS
    assert reset["steps"] == _RESET_STEPS and reset["conn"].closed


def test_delete_still_succeeds_without_touching_the_database(handler, reset, monkeypatch):
    _pin_running_code(monkeypatch)
    result = handler.handler({"RequestType": "Delete", "ResourceProperties": dict(_P0),
                              "PhysicalResourceId": "p"}, None)
    assert result == {"Status": "SUCCESS", "Data": {"Message": "Delete - no action needed"}}
    assert reset["opened"] == [] and reset["steps"] == []


def test_fix_timezone_still_returns_failed(handler, monkeypatch):
    """Pinned: the direct-invoke maintenance action is not a CloudFormation
    event; its failure stays a returned FAILED for the caller to read."""
    def unreachable():
        raise ConnectionError("secret not readable")
    monkeypatch.setattr(handler, "get_db_credentials", unreachable)
    result = handler.handler({"action": "fix_timezone"}, None)
    assert result["Status"] == "FAILED" and "secret not readable" in result["Data"]["Message"]


# --------------------------------------------------------------------------
# Full-reset Update rollback. CloudFormation rolls a failed Update back by
# sending the custom resource another Update with the PREVIOUS properties.
# The stack gives the function every custom resource property but
# ServiceToken in its environment (FULL_RESET_PINS: DB_INIT_TIMESTAMP,
# DB_NAME, DB_INIT_SEED_VERSION, DB_CLUSTER_ENDPOINT, DB_SECRET_ARN;
# tests/test_seed_cr_versions.py). A full-reset Update asking for anything
# else is logged, skipped and succeeds. Without DB_INIT_TIMESTAMP (a function
# deployed before the stack set it) every full-reset Update runs, as before.
# Create is never skipped.
# --------------------------------------------------------------------------
_ROLLBACKS = [
    # (forward old props, forward new props = the running code)
    pytest.param(_P0, _P1, id="timestamp-and-seed"),
    pytest.param({**_P1, "Timestamp": "T0"}, _P1, id="timestamp-only"),
    pytest.param({**_P1, "DatabaseName": "erp_old"}, _P1, id="rename"),
    pytest.param({**_P1, "SeedVersion": "a", "ClusterEndpoint": "old.example"}, _P1,
                 id="endpoint-and-seed"),
    # A replaced cluster or secret alone (no Timestamp or SeedVersion change)
    # is a full reset too (is_seed_only_update), so its rollback is skipped.
    pytest.param({**_P1, "ClusterEndpoint": "old.example"}, _P1, id="endpoint-only"),
    pytest.param({**_P1, "SecretArn": "arn:secret-old"}, _P1, id="secret-only"),
]


@pytest.mark.parametrize("previous, running", _ROLLBACKS)
def test_a_full_reset_rollback_on_the_new_code_skips_and_succeeds(handler, reset, monkeypatch, caplog,
                                                                  previous, running):
    _pin_running_code(monkeypatch, running)
    caplog.set_level(logging.INFO)
    rollback = _update(running, previous)
    assert not handler.is_seed_only_update(rollback)
    result = handler.handler(rollback, None)
    assert result["Status"] == "SUCCESS" and "skipped" in result["Data"]["Message"]
    assert reset["opened"] == [] and reset["steps"] == [] and reset["conn"].statements == []
    (skip,) = [r for r in caplog.records if "skipped" in r.getMessage()]
    assert skip.levelno == logging.WARNING


@pytest.mark.parametrize("previous, running", _ROLLBACKS)
def test_a_forward_full_reset_for_the_running_code_runs_and_raises(handler, reset, monkeypatch,
                                                                   previous, running):
    """The forward Update asks for the running code's properties (the stack
    computes both from the same values): the reset runs, and a failure
    raises."""
    _pin_running_code(monkeypatch, running)
    forward = _update(previous, running)
    assert handler.handler(forward, None) == _SUCCESS
    assert reset["steps"] == _RESET_STEPS
    reset["steps"].clear()
    reset["fail"] = "seed_data"
    with pytest.raises(Exception, match="boom in seed_data"):
        handler.handler(forward, None)


@pytest.mark.parametrize("env", [None, ""], ids=["unset", "empty"])
def test_without_a_pinned_timestamp_every_full_reset_runs_as_before(handler, reset, monkeypatch, env):
    """A function deployed before the stack set DB_INIT_TIMESTAMP: a
    rollback-shaped Update runs the reset, and its failure raises."""
    monkeypatch.setenv("DB_NAME", _P1["DatabaseName"])
    monkeypatch.setenv("DB_INIT_SEED_VERSION", _P1["SeedVersion"])
    if env is not None:
        monkeypatch.setenv("DB_INIT_TIMESTAMP", env)
    rollback = _update(_P1, _P0)
    assert handler.handler(rollback, None) == _SUCCESS
    assert reset["steps"] == _RESET_STEPS
    reset["fail"] = "create_schema"
    with pytest.raises(Exception, match="boom in create_schema"):
        handler.handler(rollback, None)


@pytest.mark.parametrize("props", [_P0, {**_P1, "DatabaseName": "erp_old"}], ids=["old-timestamp", "other-name"])
def test_create_is_never_skipped(handler, reset, monkeypatch, props):
    _pin_running_code(monkeypatch, _P1)
    assert handler.handler(_create(props), None) == _SUCCESS
    assert reset["steps"] == _RESET_STEPS


def test_the_dashboard_reset_all_is_never_skipped(handler, reset, monkeypatch):
    """The Streamlit Reset All invokes the function directly with
    ``{'RequestType': 'Update'}`` and no properties: not a CloudFormation
    request, so the rollback guard does not apply; it still drops and
    reseeds on a function whose stack pins the full reset, and its failure
    raises (the dashboard reports a FunctionError as a crash)."""
    _pin_running_code(monkeypatch, _P1)
    assert handler.handler(dict(_DASHBOARD_RESET_ALL), None) == _SUCCESS
    assert reset["steps"] == _RESET_STEPS
    reset["steps"].clear()
    reset["fail"] = "seed_data"
    with pytest.raises(Exception, match="boom in seed_data"):
        handler.handler(dict(_DASHBOARD_RESET_ALL), None)


# --------------------------------------------------------------------------
# The FAILED response fits CloudFormation's limit: "The total size of the
# response body can't exceed 4096 bytes" (custom resource response
# reference). The Provider (aws-cdk-lib, custom-resources/lib/
# provider-framework/runtime) sends it as JSON.stringify of {Status, Reason,
# StackId, RequestId, PhysicalResourceId, LogicalResourceId}
# (cfn-response.js submitResponse; a failed onEvent adds no Data).
# framework.js invokeUserFunction builds the Reason from the Python runtime's
# error payload ({errorMessage, errorType, requestId, stackTrace}): it joins
# errorMessage, "", "Logs: /aws/lambda/<function>", "" with "\n" and throws
# that as a JS Error. It copies the payload's "trace" (the Node.js runtime's
# field) into the stack; a Python payload has "stackTrace", so the stack is
# V8's own: "Error: <message>" and at most Error.stackTraceLimit (10) frames
# of the framework's code. A failed Create has no physical id yet, so
# safeHandler sends the CREATE_FAILED marker.
# --------------------------------------------------------------------------
_CFN_RESPONSE_LIMIT = 4096
_CREATE_FAILED_MARKER = "AWSCDK::CustomResourceProviderFramework::CREATE_FAILED"
# The longest identifiers the response can carry: a 64-character function
# name, a 128-character stack name in a long partition and region, a
# 255-character logical id.
_FUNCTION_ARN = "arn:aws-us-gov:lambda:us-gov-west-1:111122223333:function:" + "f" * 64
_LONG_STACK_ID = ("arn:aws-us-gov:cloudformation:" + "r" * 20 + ":111122223333:stack/"
                  + "S" * 128 + "/" + str(uuid.UUID(int=0)))
_LONG_LOGICAL_ID = "L" * 255
_REQUEST_ID = str(uuid.UUID(int=1))
# One stack frame, about twice the longest the real framework produces on
# Node (test_the_providers_own_failed_response_fits measures them).
_FRAME = "    at " + "x" * 193
_STACK_TRACE_LIMIT = 10

_HOSTILE_TEXTS = [
    pytest.param("😀" * 5000, id="four-byte"),
    pytest.param("工" * 5000, id="three-byte"),
    pytest.param("\x01" * 5000, id="control"),
    pytest.param('"\\' * 2500, id="quotes-and-backslashes"),
    pytest.param("\ud800" * 5000, id="lone-surrogates"),
    pytest.param("é😀\n\"工\x7f\t" * 1000, id="mixed"),
]


def _js_json_bytes(obj) -> bytes:
    """``JSON.stringify(obj)`` as UTF-8. Python's compact json.dumps with raw
    non-ASCII writes the same, except a lone surrogate, which JSON.stringify
    escapes."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    text = re.sub("[\ud800-\udfff]", lambda m: "\\u%04x" % ord(m.group()), text)
    return text.encode("utf-8")


def _provider_failed_body(error_message: str) -> bytes:
    """The FAILED response body the Provider sends for a handler that raised
    ``error_message``, with the longest identifiers and a full stack of
    oversized frames."""
    function = _FUNCTION_ARN.split(":")[-1]
    message = "\n".join([error_message, "", f"Logs: /aws/lambda/{function}", ""])
    reason = "\n".join([f"Error: {message}"] + [_FRAME] * _STACK_TRACE_LIMIT)
    return _js_json_bytes({"Status": "FAILED", "Reason": reason, "StackId": _LONG_STACK_ID,
                           "RequestId": _REQUEST_ID, "PhysicalResourceId": _CREATE_FAILED_MARKER,
                           "LogicalResourceId": _LONG_LOGICAL_ID})


def _raise_failure(handler, reset, monkeypatch, text, where):
    """Run a Create whose ``where`` (seed_data or verify_schema) fails with
    ``text``; return the raised exception info. The handler's log is off:
    the message under test is the raised one, and a lone surrogate in a
    captured log record breaks pytest-xdist's report transport."""
    monkeypatch.setattr(handler.logger, "disabled", True)
    if where == "seed_data":
        reset["fail"], reset["text"] = "seed_data", text
    else:
        monkeypatch.setattr(handler, "verify_schema", lambda conn: False)
        monkeypatch.setattr(handler, "schema_problems",
                            lambda conn: [f"No APPROVED lots for site {text}"])
    with pytest.raises(Exception) as raised:
        handler.handler(_create(), None)
    return raised


@pytest.mark.parametrize("where", ["seed_data", "verify_schema"])
@pytest.mark.parametrize("text", _HOSTILE_TEXTS)
def test_a_failure_response_fits_cloudformations_limit(handler, reset, monkeypatch, text, where):
    """A long error in any script (counted in bytes, not characters) is cut so
    the Provider's FAILED response stays under 4096 bytes; the message still
    names the step and starts with the error."""
    message = str(_raise_failure(handler, reset, monkeypatch, text, where).value)
    size = len(_provider_failed_body(message))
    assert size < _CFN_RESPONSE_LIMIT, size
    assert message.startswith(f"ERP database initialization failed during {where}: ")
    assert text[:10] in message and message.endswith("...")


def test_a_short_failure_message_is_not_cut(handler, reset):
    reset["fail"] = "seed_data"
    with pytest.raises(Exception) as raised:
        handler.handler(_create(), None)
    assert str(raised.value) == ("ERP database initialization failed during seed_data: "
                                 "RuntimeError: boom in seed_data")


_RUNTIME = "package/custom-resources/lib/provider-framework/runtime/"
# Stands in for outbound.js (the AWS SDK calls): the user function "raised"
# with the payload in PROBE_PAYLOAD; the response body goes to PROBE_BODY.
_STUB_OUTBOUND = """\
const fs = require("fs");
exports.invokeFunction = async () => ({
  FunctionError: "Unhandled", Payload: fs.readFileSync(process.env.PROBE_PAYLOAD) });
exports.httpRequest = async (_options, body) => { fs.writeFileSync(process.env.PROBE_BODY, body); };
exports.startExecution = async () => { throw new Error("no waiter in this probe"); };
"""
_DRIVER = """\
const fs = require("fs");
console.log = () => {};
const { onEvent } = require("./framework.js");
onEvent(JSON.parse(fs.readFileSync(process.env.PROBE_EVENT, "utf8"))).catch((e) => {
  process.stderr.write(String(e && e.stack)); process.exit(3); });
"""


def _provider_runtime(tmp_path: Path) -> tuple:
    """The CDK Provider framework's runtime from the installed aws-cdk-lib's
    jsii bundle (not imported: that starts jsii), with outbound.js stubbed;
    (node, directory). Skips without aws-cdk-lib or node."""
    spec = importlib.util.find_spec("aws_cdk")
    node = shutil.which("node")
    if spec is None or not spec.submodule_search_locations or node is None:
        pytest.skip("needs aws-cdk-lib and node")
    bundles = sorted(Path(spec.submodule_search_locations[0], "_jsii").glob("aws-cdk-lib@*.jsii.tgz"))
    if not bundles:
        pytest.skip("no aws-cdk-lib jsii bundle")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with tarfile.open(bundles[-1]) as tar:
        for name in ("framework.js", "cfn-response.js", "util.js", "consts.js"):
            (runtime / name).write_bytes(tar.extractfile(_RUNTIME + name).read())
    (runtime / "outbound.js").write_text(_STUB_OUTBOUND)
    (runtime / "driver.js").write_text(_DRIVER)
    return node, runtime


def _run_provider(node: str, runtime: Path, raised) -> bytes:
    """Run the framework's onEvent for a Create whose handler raised
    ``raised`` (the Python runtime's error payload); return the body it PUTs
    to CloudFormation."""
    exc = raised.value
    payload = {"errorMessage": str(exc), "errorType": type(exc).__name__, "requestId": _REQUEST_ID,
               "stackTrace": traceback.format_tb(raised.tb)}
    event = {"RequestType": "Create", "RequestId": _REQUEST_ID, "StackId": _LONG_STACK_ID,
             "ResponseURL": "https://cloudformation-custom-resource-response-useast1.s3.amazonaws.com/r?s",
             "ResourceType": "AWS::CloudFormation::CustomResource",
             "LogicalResourceId": _LONG_LOGICAL_ID, "ResourceProperties": dict(_P1)}
    (runtime / "payload.json").write_text(json.dumps(payload))   # ASCII; JSON.parse decodes it
    (runtime / "event.json").write_text(json.dumps(event))
    env = {"PATH": os.environ.get("PATH", ""), "USER_ON_EVENT_FUNCTION_ARN": _FUNCTION_ARN,
           "PROBE_PAYLOAD": str(runtime / "payload.json"), "PROBE_EVENT": str(runtime / "event.json"),
           "PROBE_BODY": str(runtime / "body.json")}
    done = subprocess.run([node, "driver.js"], cwd=runtime, env=env, capture_output=True,  # nosec B603
                          text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return (runtime / "body.json").read_bytes()


@pytest.mark.parametrize("text", _HOSTILE_TEXTS)
def test_the_providers_own_failed_response_fits(handler, reset, monkeypatch, tmp_path, text):
    """The real Provider framework code, run on node with the raised error:
    its response is FAILED, carries the message, stays under 4096 bytes, and
    its stack has at most 10 frames none longer than _FRAME (so
    _provider_failed_body is an upper bound)."""
    node, runtime = _provider_runtime(tmp_path)
    raised = _raise_failure(handler, reset, monkeypatch, text, "seed_data")
    body = _run_provider(node, runtime, raised)
    response = json.loads(body)
    assert response["Status"] == "FAILED"
    assert response["PhysicalResourceId"] == _CREATE_FAILED_MARKER
    assert len(body) < _CFN_RESPONSE_LIMIT, len(body)
    reason = response["Reason"]
    assert reason.startswith(f"Error: {raised.value}\n\nLogs: /aws/lambda/{_FUNCTION_ARN.split(':')[-1]}\n")
    # The deployed framework runs from /var/task, not this test's directory
    # (node reports the resolved path).
    frames = [line.replace(str(runtime.resolve()), "/var/task") for line in reason.split("\n")
              if line.startswith("    at ")]
    assert 0 < len(frames) <= _STACK_TRACE_LIMIT
    assert max(len(_js_json_bytes(f)) for f in frames) <= len(_FRAME) // 2, frames


# --------------------------------------------------------------------------
# Real PostgreSQL (throwaway database): ERP_SIM_TEST_PG_DSN; skipped otherwise.
# --------------------------------------------------------------------------
_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture
def pg_db(handler, monkeypatch):
    """A throwaway database the handler connects to (get_connection) and
    resets (DB_NAME, which drop_all_tables uses to end OTHER sessions on that
    database only); dropped afterwards. Yields a function opening a new
    connection to it."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    db_name = "dbi_" + uuid.uuid4().hex[:8]
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_NAME", db_name)
    dsn = make_dsn(**{**parse_dsn(_DSN), "dbname": db_name})
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=db_name))

    def connect(_credentials=None):
        assert os.environ["DB_NAME"] == db_name  # never end sessions on another database
        return pgdriver.connect(dsn)

    monkeypatch.setattr(handler, "get_db_credentials", lambda: {})
    monkeypatch.setattr(handler, "get_connection", connect)
    try:
        yield connect
    finally:
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (db_name,))
            cur.execute(stmt("DROP DATABASE {db}", db=db_name))
        admin.close()


def _scalar(connect, sql):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_pg_create_and_a_second_reset_succeed_unchanged(handler, pg_db):
    """Pinned: a Create on an empty database, then a full-reset Update over
    the seeded one, both return the same success and leave the seed."""
    assert handler.handler(_create(), None) == _SUCCESS
    assert handler.handler(_update(_P0, _P1), None) == _SUCCESS
    assert _scalar(pg_db, "SELECT count(*) FROM material_lots WHERE quality_status = 'APPROVED'") > 0


def test_pg_create_raises_when_the_schema_cannot_be_created(handler, pg_db):
    """A stale VIEW named like an ERP table survives drop_all_tables (it
    drops tables only); CREATE TABLE IF NOT EXISTS skips it and the index on
    it fails: the Create raises naming create_schema and PostgreSQL's error."""
    conn = pg_db()
    with conn.cursor() as cur:
        cur.execute("CREATE VIEW public.customers AS SELECT 1 AS stale")
    conn.commit()
    conn.close()
    with pytest.raises(Exception) as raised:
        handler.handler(_create(), None)
    message = str(raised.value)
    assert "create_schema" in message and "customers" in message


def test_pg_create_raises_naming_a_table_the_reset_lost(handler, pg_db, monkeypatch):
    real_seed = handler.seed_data

    def seed_then_lose_a_table(conn):
        real_seed(conn)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE item_suppliers CASCADE")
        conn.commit()
    monkeypatch.setattr(handler, "seed_data", seed_then_lose_a_table)
    with pytest.raises(Exception) as raised:
        handler.handler(_create(), None)
    assert "item_suppliers" in str(raised.value)


def test_pg_create_raises_naming_a_lost_mes_equipment_registry(handler, pg_db, monkeypatch):
    real_seed = handler.seed_data

    def seed_then_lose_the_registry(conn):
        real_seed(conn)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE mes_equipment CASCADE")
        conn.commit()
    monkeypatch.setattr(handler, "seed_data", seed_then_lose_the_registry)
    with pytest.raises(Exception) as raised:
        handler.handler(_create(), None)
    assert "mes_equipment" in str(raised.value)


def test_pg_create_raises_naming_a_site_left_without_approved_lots(handler, pg_db, monkeypatch):
    real_seed = handler.seed_data

    def seed_then_quarantine_detroit(conn):
        real_seed(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE material_lots SET quality_status = 'QUARANTINE' "
                        "WHERE lot_number LIKE 'LOT-DET-%'")
        conn.commit()
    monkeypatch.setattr(handler, "seed_data", seed_then_quarantine_detroit)
    with pytest.raises(Exception) as raised:
        handler.handler(_create(), None)
    message = str(raised.value)
    assert "DETROIT_AUTOPARTS" in message and "MILWAUKEE_BOTTLING" not in message
