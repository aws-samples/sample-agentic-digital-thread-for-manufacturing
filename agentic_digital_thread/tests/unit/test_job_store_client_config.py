# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``JobStore(client_config=...)``: an optional botocore ``Config`` for the
DynamoDB resource the store builds for its shared table, on both the
fail-soft path (``_get_table``) and the strict one (``_strict_table``).

Without it (the default, every existing kind) the resource is built exactly
as before: ``boto3.resource("dynamodb", region_name=...)`` with no config, so
botocore's defaults apply. The built clients are real boto3 clients, read
through ``meta.config``; nothing is sent (building a client opens no socket).
"""
from __future__ import annotations

import boto3
import pytest
from botocore.config import Config

from agents.job_store import JobStore

OPENERS = ["_strict_table", "_get_table"]


@pytest.fixture
def table_env(monkeypatch):
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "adt-onboarding-sessions")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    return monkeypatch


def _spy_resource(monkeypatch) -> list:
    calls: list = []
    real = boto3.resource

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)
    monkeypatch.setattr(boto3, "resource", spy)
    return calls


def _config_of(table):
    config = table.meta.client.meta.config
    return config.connect_timeout, config.read_timeout, config.retries


@pytest.mark.parametrize("opener", OPENERS)
def test_without_a_client_config_the_resource_is_built_as_before(table_env, opener):
    default = _config_of(boto3.resource("dynamodb", region_name="us-east-1")
                         .Table("adt-onboarding-sessions"))
    calls = _spy_resource(table_env)
    table = getattr(JobStore("plain-kind"), opener)()
    assert calls == [(("dynamodb",), {"region_name": "us-east-1"})]
    assert _config_of(table) == default


@pytest.mark.parametrize("opener", OPENERS)
def test_a_client_config_reaches_the_built_client(table_env, opener):
    config = Config(connect_timeout=1, read_timeout=3,
                    retries={"total_max_attempts": 2, "mode": "standard"})
    calls = _spy_resource(table_env)
    table = getattr(JobStore("bounded-kind", client_config=config), opener)()
    assert len(calls) == 1 and calls[0][1]["config"] is config
    assert _config_of(table) == (1, 3, {"total_max_attempts": 2, "mode": "standard"})


def test_an_injected_table_is_used_as_given(table_env):
    calls = _spy_resource(table_env)
    table = object()
    store = JobStore("bounded-kind", table=table, client_config=Config(read_timeout=3))
    assert store._strict_table() is table and store._get_table() is table
    assert calls == []


def test_a_client_config_changes_nothing_without_a_table(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    calls = _spy_resource(monkeypatch)
    store = JobStore("bounded-kind", client_config=Config(read_timeout=3))
    store["a"] = {"status": "running"}
    assert store.get("a") == {"status": "running"}
    assert store._strict_table() is None and calls == []
