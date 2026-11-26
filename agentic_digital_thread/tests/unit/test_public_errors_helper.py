# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The public-error helper: categories from types only, safe passthrough, total.

Offline: exceptions are constructed locally; no network.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from agents import public_errors as pe


def _client_error(code: str, status: int = 400) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "arn:aws:iam::111122223333:x"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "Op")


def _http_status(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example.com/x")
    return httpx.HTTPStatusError("bad", request=request,
                                 response=httpx.Response(status, request=request))


@pytest.mark.parametrize("exc, category", [
    (_client_error("AccessDeniedException"), "auth_failed"),
    (_client_error("ExpiredTokenException"), "auth_failed"),
    (_client_error("ResourceNotFoundException"), "not_found"),
    (_client_error("ThrottlingException"), "upstream_error"),
    (_client_error("InternalServerException", 500), "upstream_error"),
    (_http_status(401), "auth_failed"),
    (_http_status(403), "auth_failed"),
    (_http_status(404), "not_found"),
    (_http_status(504), "timed_out"),
    (_http_status(503), "upstream_error"),
    (httpx.ConnectError("refused"), "not_reachable"),
    (httpx.ReadTimeout("slow"), "timed_out"),
    (ReadTimeoutError(endpoint_url="https://x.example.com"), "timed_out"),
    (EndpointConnectionError(endpoint_url="https://x.example.com"), "not_reachable"),
    (TimeoutError("slow"), "timed_out"),
    (ConnectionRefusedError("refused"), "not_reachable"),
    (json.JSONDecodeError("Expecting value", "x", 0), "invalid_response"),
])
def test_category_comes_from_type_code_and_status(exc, category):
    assert pe.classify(exc) == category


def test_graph_query_errors_are_graph_query_failed():
    from agents.db import GraphQueryError
    assert pe.classify(GraphQueryError("ValidationException", "bad cypher")) == "graph_query_failed"


def test_postgres_auth_and_unreachable_are_told_apart_by_pgcode():
    from agents import pgdriver

    auth = pgdriver.OperationalError("password authentication failed", pgcode="28P01")
    assert pe.classify(auth) == "auth_failed"
    assert pe.classify(pgdriver.OperationalError("could not connect")) == "not_reachable"


def test_the_message_text_never_picks_the_category():
    exc = RuntimeError("401 Unauthorized: timed out, not found, connection refused")
    assert pe.classify(exc) == "internal_error"
    assert pe.classify(exc, default="graph_query_failed") == "graph_query_failed"


def test_an_unknown_default_falls_back_to_internal_error():
    assert pe.classify(RuntimeError("x"), default="nonsense") == "internal_error"


def test_public_error_shape_and_log_line(caplog):
    caplog.set_level(logging.WARNING, logger="agents.public_errors")
    out = pe.public_error(_client_error("AccessDeniedException"), "unit.test")
    assert out["error_category"] == "auth_failed"
    assert re.fullmatch(r"[0-9a-f]{12}", out["error_ref"])
    assert out["error"] == f"{pe.PUBLIC_ERROR_MESSAGES['auth_failed']} Reference: {out['error_ref']}"
    assert "arn:" not in out["error"]
    record = next(r for r in caplog.records if f"ref={out['error_ref']}" in r.getMessage())
    line = record.getMessage()
    assert "where=unit.test" in line and "category=auth_failed" in line
    assert "code=AccessDeniedException" in line
    assert "111122223333" not in line
    assert record.exc_info is None


def test_a_forced_category_wins():
    out = pe.public_error(RuntimeError("x"), "unit.test", category="graph_query_failed")
    assert out["error_category"] == "graph_query_failed"


def test_references_differ_per_call():
    refs = {pe.public_error(RuntimeError("x"), "unit.test")["error_ref"] for _ in range(20)}
    assert len(refs) == 20


def test_a_safe_typed_error_keeps_its_message():
    from agents.connection_security import ConnectionTargetError
    from agents.onboard import ResetIncompleteError
    assert pe.public_error(ResetIncompleteError("retry the removal"), "t") == {
        "error": "retry the removal"}
    assert pe.public_error(ConnectionTargetError("host did not resolve"), "t") == {
        "error": "host did not resolve"}


def test_a_subclass_of_a_safe_type_is_not_trusted():
    from agents.onboard import ResetIncompleteError

    class _Wrapped(ResetIncompleteError):
        pass
    out = pe.public_error(_Wrapped("arn:aws:iam::111122223333:role/x"), "t")
    assert "arn:" not in out["error"] and out["error_ref"]


def test_every_safe_type_resolves_to_a_real_class():
    import importlib
    for qualified in pe.SAFE_MESSAGE_TYPES:
        module, _, name = qualified.rpartition(".")
        assert isinstance(getattr(importlib.import_module(module), name), type), qualified


def test_scrub_runs_before_the_message_is_logged(caplog):
    caplog.set_level(logging.WARNING, logger="agents.public_errors")
    out = pe.public_error(RuntimeError("token=hunter-two at host.example.com"), "t",
                          scrub=lambda text: text.replace("hunter-two", "***"))
    line = next(r.getMessage() for r in caplog.records if out["error_ref"] in r.getMessage())
    assert "hunter-two" not in line and "host.example.com" in line


def test_an_unprintable_exception_still_gets_a_public_error():
    class _Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("no")
    out = pe.public_error(_Unprintable(), "t")
    assert out["error_category"] == "internal_error" and out["error_ref"]
