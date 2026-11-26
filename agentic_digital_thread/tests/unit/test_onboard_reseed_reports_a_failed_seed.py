# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""POST /api/onboard/reseed (Reset All) reports a seed function that failed.

The route invokes the ``adt-seed-data`` function synchronously. A function
that RAISED (lambda-adt-seed/handler.py raises when a seed file fails) still
answers the invoke call with HTTP 200: the failure is the response's
``FunctionError`` and an error payload. The route must answer
``{"status": "error", ...}`` then, in the public_error shape (a fixed
message plus a reference; the payload's text goes only to the server log),
so Reset All (frontend/src/views/onboard/flow.ts) shows the failure.

The invoke payload carries no StackId and no Version, so the seed handler's
update-rollback guard never skips it.

Offline: boto3.client is replaced by a fake Lambda client.
"""
import asyncio
import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("GRAPH_IDENTIFIER", "g-unittest")

import agents.onboard as onboard  # noqa: E402
from agents.public_errors import PUBLIC_ERROR_MESSAGES  # noqa: E402

# What lambda-adt-seed/handler.py's raise looks like to the invoke caller (the
# Python runtime's error payload), with the backend text a browser must not see.
_SEED_ERROR = {
    "errorMessage": ("Demo seed failed: 1 of 10 SQL files failed: 005_plm_engineering.sql. First error, "
                     "in 005_plm_engineering.sql: UniqueViolation: duplicate key value violates unique "
                     "constraint \"products_pkey\" on adt-demo.cabc123.us-east-1.rds.amazonaws.com"),
    "errorType": "SeedLoadError",
    "requestId": "00000000-0000-0000-0000-000000000001",
    "stackTrace": ["  File \"/var/task/handler.py\", line 120, in handler\n"],
}
_LEAKS = ("UniqueViolation", "products_pkey", "rds.amazonaws.com", "005_plm", "/var/task", "SeedLoadError")


class _FakeLambda:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def fake_lambda(monkeypatch):
    import boto3

    holder = {}

    def client(service, region_name=None):
        assert service == "lambda"
        return holder["client"]

    monkeypatch.setattr(boto3, "client", client)
    # A seed-enabled deployment: the compute stack sets the function name.
    monkeypatch.setenv("ADT_SEED_LAMBDA", "adt-seed-data")
    return holder


def _reseed():
    return asyncio.run(onboard.onboard_reseed())


def _response(payload: bytes, function_error=None) -> dict:
    response = {"StatusCode": 200, "Payload": io.BytesIO(payload)}
    if function_error:
        response["FunctionError"] = function_error
    return response


@pytest.mark.parametrize("payload", [
    pytest.param(json.dumps(_SEED_ERROR).encode(), id="seed-error-payload"),
    pytest.param(b"not json", id="unreadable-payload"),
])
def test_a_seed_function_that_raised_is_an_error(fake_lambda, caplog, payload):
    """FunctionError -> status error, upstream_error, a reference; none of the
    payload's text reaches the response, and the log line carries the
    reference."""
    fake_lambda["client"] = _FakeLambda(_response(payload, "Unhandled"))
    caplog.set_level(logging.WARNING)
    result = _reseed()
    assert result["status"] == "error", result
    assert result["error_category"] == "upstream_error"
    assert result["error"] == f"{PUBLIC_ERROR_MESSAGES['upstream_error']} Reference: {result['error_ref']}"
    body = json.dumps(result)
    for leak in _LEAKS:
        assert leak not in body
    assert any(f"PUBLIC_ERROR ref={result['error_ref']} where=onboard.reseed" in r.getMessage()
               for r in caplog.records)


def test_a_seed_that_succeeded_is_ok(fake_lambda):
    payload = json.dumps({"PhysicalResourceId": "vf-seed", "Data": {"Status": "Complete"}}).encode()
    fake_lambda["client"] = _FakeLambda(_response(payload))
    assert _reseed() == {"status": "ok", "lambda_status": 200}


def test_an_invoke_that_failed_is_an_error(fake_lambda):
    fake_lambda["client"] = _FakeLambda(error=RuntimeError("could not connect to lambda.example"))
    result = _reseed()
    assert result["status"] == "error" and "lambda.example" not in json.dumps(result)


def test_the_reseed_invoke_is_never_taken_for_an_update_rollback(fake_lambda):
    """A synchronous invoke of the seed function with no StackId and no
    Version: the handler's rollback guard compares only CloudFormation
    requests that carry a Version, so this always re-runs every file."""
    client = _FakeLambda(_response(b"{}"))
    fake_lambda["client"] = client
    _reseed()
    (call,) = client.calls
    assert call["FunctionName"] == "adt-seed-data"
    assert call["InvocationType"] == "RequestResponse"
    event = json.loads(call["Payload"])
    assert event == {"RequestType": "Update", "ResourceProperties": {}}


@pytest.mark.parametrize("value", [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param("  ", id="blank"),
])
def test_a_deployment_without_the_sample_database_skips_the_reseed(fake_lambda, monkeypatch, value):
    """A base deploy has no ADTSeedDataStack, so the compute stack sets no
    ADT_SEED_LAMBDA: Reset All succeeds without an invoke instead of reporting
    that sample databases it never had were not reseeded."""
    if value is None:
        monkeypatch.delenv("ADT_SEED_LAMBDA", raising=False)
    else:
        monkeypatch.setenv("ADT_SEED_LAMBDA", value)
    client = _FakeLambda(_response(b"{}"))
    fake_lambda["client"] = client
    assert _reseed() == {"status": "skipped", "reason": "no_sample_database"}
    assert client.calls == []
