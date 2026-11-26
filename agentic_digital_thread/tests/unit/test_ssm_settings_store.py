# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ``storage.SsmSettingsStore`` (S4.4b demo runtime-toggle).

``SsmSettingsStore`` is the duck-typed persister (``get_flag``/``set_flag``)
backing the demo seam's runtime override. Reads are boot-resilient (never crash
over a settings read → return None on error); writes are fail-loud (the admin
PUT must surface a failed persist). ``boto3.client`` is monkeypatched so no real
SSM/AWS/network is touched.
"""

import logging

import pytest
from botocore.exceptions import ClientError

import agents.storage as storage
from agents.storage import SsmSettingsStore


class _FakeSsmClient:
    def __init__(self):
        self.get_parameter_result = None
        self.get_parameter_error = None
        self.put_parameter_error = None
        self.put_calls = []

    def get_parameter(self, **kwargs):
        if self.get_parameter_error is not None:
            raise self.get_parameter_error
        return self.get_parameter_result

    def put_parameter(self, **kwargs):
        self.put_calls.append(kwargs)
        if self.put_parameter_error is not None:
            raise self.put_parameter_error
        return {}


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeSsmClient()
    monkeypatch.setattr(storage.boto3, "client", lambda *a, **k: client)
    return client


def _client_error(code: str, operation: str = "GetParameter") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


# ---------------------------------------------------------------------------
# get_flag — parsing + boot-resilient error handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("false", False), ("TRUE", True), (" true ", True),
     ("1", True), ("yes", True), ("on", True), ("0", False), ("no", False)],
)
def test_get_flag_parses_value(fake_client, raw, expected):
    fake_client.get_parameter_result = {"Parameter": {"Value": raw}}
    store = SsmSettingsStore("/adt/demo-enabled")
    assert store.get_flag() is expected


def test_get_flag_parameter_not_found_returns_none(fake_client):
    fake_client.get_parameter_error = _client_error("ParameterNotFound")
    store = SsmSettingsStore("/adt/demo-enabled")
    assert store.get_flag() is None


def test_get_flag_other_client_error_returns_none_and_warns(fake_client, caplog):
    fake_client.get_parameter_error = _client_error("AccessDeniedException")
    store = SsmSettingsStore("/adt/demo-enabled")
    with caplog.at_level(logging.WARNING, logger=storage.logger.name):
        assert store.get_flag() is None
    assert any("get_flag" in r.message for r in caplog.records)


def test_get_flag_generic_error_returns_none_and_warns(fake_client, caplog):
    fake_client.get_parameter_error = RuntimeError("network down")
    store = SsmSettingsStore("/adt/demo-enabled")
    with caplog.at_level(logging.WARNING, logger=storage.logger.name):
        assert store.get_flag() is None
    assert any("get_flag" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# set_flag — Overwrite=True + fail-loud propagation
# ---------------------------------------------------------------------------

def test_set_flag_true_calls_put_parameter(fake_client):
    store = SsmSettingsStore("/adt/demo-enabled")
    store.set_flag(True)
    assert len(fake_client.put_calls) == 1
    kwargs = fake_client.put_calls[0]
    assert kwargs["Name"] == "/adt/demo-enabled"
    assert kwargs["Value"] == "true"
    assert kwargs["Type"] == "String"
    assert kwargs["Overwrite"] is True


def test_set_flag_false_writes_false(fake_client):
    store = SsmSettingsStore("/adt/demo-enabled")
    store.set_flag(False)
    assert fake_client.put_calls[0]["Value"] == "false"
    assert fake_client.put_calls[0]["Overwrite"] is True


def test_set_flag_propagates_on_error(fake_client):
    fake_client.put_parameter_error = _client_error("AccessDeniedException", "PutParameter")
    store = SsmSettingsStore("/adt/demo-enabled")
    with pytest.raises(ClientError):
        store.set_flag(True)


# ---------------------------------------------------------------------------
# get_interval / set_interval — the optional second (interval) parameter
# ---------------------------------------------------------------------------

def _interval_store() -> SsmSettingsStore:
    return SsmSettingsStore("/adt/live-updates-enabled",
                            interval_param_name="/adt/live-updates-interval")


def test_get_interval_parses_int(fake_client):
    fake_client.get_parameter_result = {"Parameter": {"Value": " 300 "}}
    assert _interval_store().get_interval() == 300


def test_get_interval_non_numeric_returns_none_and_warns(fake_client, caplog):
    fake_client.get_parameter_result = {"Parameter": {"Value": "soon"}}
    with caplog.at_level(logging.WARNING, logger=storage.logger.name):
        assert _interval_store().get_interval() is None
    assert any("get_interval" in r.message for r in caplog.records)


def test_get_interval_parameter_not_found_returns_none(fake_client):
    fake_client.get_parameter_error = _client_error("ParameterNotFound")
    assert _interval_store().get_interval() is None


def test_get_interval_read_error_returns_none_and_warns(fake_client, caplog):
    fake_client.get_parameter_error = _client_error("AccessDeniedException")
    with caplog.at_level(logging.WARNING, logger=storage.logger.name):
        assert _interval_store().get_interval() is None
    assert any("get_interval" in r.message for r in caplog.records)


def test_get_interval_without_configured_param_returns_none(fake_client):
    assert SsmSettingsStore("/adt/live-updates-enabled").get_interval() is None


def test_set_interval_writes_second_parameter(fake_client):
    _interval_store().set_interval(300)
    assert len(fake_client.put_calls) == 1
    kwargs = fake_client.put_calls[0]
    assert kwargs["Name"] == "/adt/live-updates-interval"
    assert kwargs["Value"] == "300"
    assert kwargs["Type"] == "String"
    assert kwargs["Overwrite"] is True


def test_set_interval_propagates_on_error(fake_client):
    fake_client.put_parameter_error = _client_error("AccessDeniedException", "PutParameter")
    with pytest.raises(ClientError):
        _interval_store().set_interval(300)


def test_set_interval_without_configured_param_raises(fake_client):
    with pytest.raises(ValueError):
        SsmSettingsStore("/adt/live-updates-enabled").set_interval(300)
